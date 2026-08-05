# ISSUE / PR 提交流程 — 问题定位后自动提交

> **渐进式披露**：本文件只在「用户同意提交 ISSUE/PR」后才读取（见 SKILL.md 的
> Debugging Workflow 第 6 步）。平时不要加载本文件——它包含 API 调用细节，与日常开发无关。

## 触发时机

在以下流程完成后触发（由 SKILL.md 的 Debugging Workflow 驱动）：

1. Bug 已定位、修复完成、测试拦截通过（测试绿）
2. 案例已固化到 `bug-fix-history/`（INDEX.md + 案例文件）

此时向用户提问是否提交 ISSUE，**仅在用户同意后**读取本文件并执行。也适用于用户直接要求的清理/重构类 ISSUE+PR（如删除冗余代码）。

## 流程顺序（硬性）

**先提 ISSUE → 再提 PR → PR 正文直接写 ISSUE 链接**（仓库 PR 模板要求关联 `#ISSUE ID`；
用 `Fixes #ID` 会自动关闭 issue，仅当 PR 完全解决该 issue 时使用，否则用「关联 #ID」）。
创建 PR 前必须先拿到 ISSUE 编号，禁止先 PR 后补 ISSUE。

## ① 防重复检查（提交前必须执行）

**有相似历史 issue 就绝不提交**——重复 issue 会污染仓库、浪费维护者时间。检查逻辑：

1. 用案例的关键词 + 核心症状搜索历史 issue：

   ```bash
   # 关键词搜索（GitCode 兼容 Gitee API v5）
   curl -sS -H "private-token: $GITCODE_TOKEN" "https://gitcode.com/api/v5/search/issues?q=repo:Ascend/MindIE-Motor+<关键词1>+<关键词2>"
   ```

   `search/issues` 无结果时，再用全量列表 + 本地过滤兜底：

   ```bash
   # 拉取全部 issue（含已关闭），本地按标题/正文匹配关键词
   curl -sS -H "private-token: $GITCODE_TOKEN" "https://gitcode.com/api/v5/repos/Ascend/MindIE-Motor/issues?state=all&per_page=100&page=1"
   ```

2. **重复判定**：历史 issue 的标题或正文命中案例的**关键词**（3-5 个）中 ≥2 个，或命中**核心症状**（如报错码、异常特征），即视为相似。

3. **命中即终止**：告知用户「已有类似 issue：#<编号> <标题>（<链接>）」，附上对比说明（相似点/差异点），**不提交**，流程结束。若差异确实重大（如不同模块、不同版本、新场景），先向用户说明差异、由用户决定是否仍要提交。

4. **未命中**：继续生成草稿。

## ② GitCode API 关键细节（踩坑记录，务必遵守）

- **鉴权**：所有写操作（创建/更新 issue、PR）必须带 `private-token: $GITCODE_TOKEN` header。
  `access_token` 放 body 或 query 会报 `Invalid header parameter: private-token, required`。
- **创建 issue 时不要带 `labels` 字段**：带 labels（数组或字符串形式）会返回
  `403 CH.00000403 apig token has not permission to request url`——与 token 权限无关，
  去掉 labels 即成功。需要标签时在网页端手动补。
- **403 报错先隔离字段，不要急着下「token 权限不足」的结论**：上述 403 文本有误导性。
  排查顺序：去掉可选字段（labels 等）→ 换鉴权方式 → 再考虑 token 权限。
- **PATCH 更新 PR/issue 是整体替换**：只传要改的字段会清空其余内容。必须用完整正文
  （原正文 + 追加改动）整体更新。
- **创建响应中 `html_url` 可能为 null**：从 `number` 字段拿编号，web 地址为
  `https://gitcode.com/<owner>/<repo>/issues/<number>`。
- **PR 跨 fork 提交**：head 格式为 `fork_owner/fork_repo:fork_branch`（如 `<我的账号>/<fork仓库>:<分支名>`），
  base 为目标分支；web 地址为 `.../merge_requests/<number>`（不是 /pulls）。
- **Token 位置**：`GITCODE_TOKEN` 从环境变量读取（写入 `~/.bashrc`），绝不写死在脚本/文档里。

## ③ 提交 ISSUE

```bash
# 创建 issue（bug-report 模板：title 前缀 + 不带 labels）
curl -sS -X POST "https://gitcode.com/api/v5/repos/Ascend/MindIE-Motor/issues" \
  -H "private-token: $GITCODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$(jq -n \
    --arg title "[Bug-Report|缺陷反馈]: <标题>" \
    --arg body "<markdown 正文>" \
    '{title: $title, body: $body}')"
```

响应中的 `number` 即 ISSUE 编号，报告给用户并进入下一步。

## ④ 提交 PR（ISSUE 创建成功后）

1. 按仓库 `.gitcode/PULL_REQUEST_TEMPLATE.md` 组织正文（合入背景/修改内容/资料变更/接口变更/测试结果/CheckList），末尾写 `关联 ISSUE：#<number>`（或链接）。
2. 创建：

   ```bash
   curl -sS -X POST "https://gitcode.com/api/v5/repos/Ascend/MindIE-Motor/pulls" \
     -H "private-token: $GITCODE_TOKEN" \
     -H "Content-Type: application/json" \
     -d "$(jq -n \
       --arg title "<PR标题>" \
       --arg head "<fork账号>/<fork仓库>:<分支名>" \
       --arg base "master" \
       --arg body "<完整正文，含 ISSUE 链接>" \
       '{title: $title, head: $head, base: $base, body: $body}')"
   ```

3. 若需修改 PR 正文，**必须传完整正文**（PATCH 是整体替换，见 ②）。

## 路径 B：生成 markdown 草稿（无需 token）

当用户不想提供 token 时：

1. 按模板字段生成草稿，写入 `docs/issue_drafts/<YYYY-MM-DD>-<short-title>.md`（仓库根下，方便用户打开；**先 `mkdir -p docs/issue_drafts`**，该目录不随仓库存在；**不要 git add**，避免混入 PR 分支）。
2. 草稿内容 = bug-report 模板的完整填写版（含「搜索确认」勾选提示）。
3. 提示用户：打开 <https://gitcode.com/Ascend/MindIE-Motor/issues/new> → 选择「🐛 Bug-Report|缺陷反馈」模板 → 粘贴草稿内容。

## 结束条件

- 提交成功 → 报告 ISSUE 和 PR 的 web 地址，收尾
- 生成草稿 → 告知文件路径，收尾
- 用户拒绝 → 静默结束（案例已固化，知识不丢失）
