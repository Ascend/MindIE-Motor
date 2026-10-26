# 原生 DeepSeek renderer 选择

- **现象**：原生 tokenizer 无 Jinja 模板属性时，缓存亲和编码改用手工 marker。
- **根因**：将 chat_template 属性当成自定义 apply_chat_template 可用性的判断条件。
- **为什么会写出**：把 Hugging Face 模板约定泛化到具有原生 renderer 的 tokenizer。
- **修复**：记录原生 vLLM tokenizer 来源，使用其 renderer，并可选加载匹配的 Ascend thinking 适配。
- **测试拦截**：无模板原生 renderer 的工具与思考参数矩阵，以及可选适配器缺失和内部依赖错误。
- **场景**：DeepSeek V4 本地 vLLM tokenizer；SGLang 与 AutoTokenizer 回退保持原义。
- **关键词**：tokenizer, DeepSeek, chat_template, renderer, thinking。
