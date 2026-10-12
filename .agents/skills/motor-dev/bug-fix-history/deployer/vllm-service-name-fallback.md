# [2026-10-12] vLLM 可选服务名被 Render 强制要求

- **现象 (Symptom)**：仅配置 model 的合法 vLLM 配置在 Render 部署时报 served_model_name 非空错误，其他路径得到空值或 Unknown。
- **根因 (Root cause)**：Render、运行时 resolver 和三个部署辅助解析器缺少向 model 的回退。
- **为什么会写出 (Why)**：将显式服务别名当作必填字段，没有对齐引擎默认值。
- **修复 (Fix)**：vLLM 路径保留原有显式名称与旧配置优先级，仅在名称缺省或为空时回退到完整模型路径。
- **测试拦截 (Test interception)**：配置解析、Render 及三个部署解析入口分别覆盖空值回退；配置与部署模块 379 个测试通过。
- **场景 (Scenario)**：使用 vLLM，省略 served_model_name 或将其设为空值。
- **关键词 (Keywords)**：vLLM, served_model_name, Render, resolver, model fallback
