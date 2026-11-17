# [2026-10-08] 模型目录上移后文档链接门禁失败

- **现象 (Symptom)**：模型目录迁移 PR 的文档门禁报告两处本地路径越出仓库，版本分支还报告指向上游 master 新路径的 HTTP 链接不可访问；CRLF 被归一化使 PR 新增行数超过门禁阈值。
- **根因 (Root cause)**：`kvcache_affinity.md` 的链接多了一层 `../`；绝对链接校验访问尚未合入的上游路径；移动文件后 Git 将旧 JSON 视为新增文件并应用 LF 归一化规则。
- **为什么会写出 (Why)**：只验证部署器和 pre-commit，未检查文档链接的实际目标、站点渲染结果及迁移前后的字节差异。
- **修复 (Fix)**：文档源码采用能在当前检出中解析的相对链接；`docs/mkdocs/hooks/model_config_links.py` 在站点构建时验证目标并转换为当前 Read the Docs Git 版本的仓库链接，PR 预览使用提交 SHA。`.gitattributes` 为模型 JSON 保留原换行符，避免目录迁移夹带格式转换。
- **测试拦截 (Test interception)**：`tests/docs/test_model_config_links.py` 遍历真实文档，验证 master/v3.2.0 链接和错误路径拦截，覆盖 PR 预览、代码块与普通文档链接；另通过实际 MkDocs 构建检查输出链接，并比较模型 JSON 内容及迁移差异。
- **场景 (Scenario)**：文档引用 `docs/zh` 外的模型配置，目录重排后需要兼容仓库浏览、PR 门禁及分版本站点。
- **关键词 (Keywords)**：model_configs, link-validity-check, MkDocs, CRLF, 目录迁移

## 2026-11-17 特性文档迁移后的反向链接和站点链接

- 模型配置 README 在上游新增后仍使用旧的 KV Cache Store 路径。同步上游时，用仓库相对路径指向迁移后的 `memcache.md`。
- 特性支持表使用 HTML 链接。MkDocs 钩子按目标文档重写相对链接，并保留 HTML 属性、查询参数和页内锚点。
- `tests/docs/test_feature_support_links.py` 覆盖链接格式、缺失目标和非特性页。用 MkDocs 实际构建验证特性支持表。
