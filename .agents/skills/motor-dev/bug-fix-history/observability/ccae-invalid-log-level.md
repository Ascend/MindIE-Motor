# [2026-10-14] CCAE Reporter 非法日志级别静默回退

- **现象 (Symptom)**：`CCAE_REPORTER_LOG_LEVEL=bogus` 时 Reporter 使用 INFO，但没有配置错误提示。
- **根因 (Root cause)**：`examples/features/observability/ccae_reporter/common/logging.py` 的 `init_logger()` 仅处理合法级别，非法值直接保留默认值。
- **为什么会写出 (Why)**：只校验配置是否可用，遗漏了回退时的诊断信息；检视正则问题时应先验证具体反例，不能直接接受误匹配推断。
- **修复 (Fix)**：非法非空级别通过标准 logging 输出 warning，包含非法值与 INFO 回退说明；未设置或空白值继续静默使用默认值。现有采集正则保持不变。
- **测试拦截 (Test interception)**：`tests/examples/observability/test_ccae_reporter_logging.py` 验证级别解析、别名、默认值、非法值回退和告警；通过创建文件事件验证轮转/临时文件被排除，identity 中连字符和正则特殊字符按字面匹配。旧代码仅非法级别告警用例失败，修复后单文件 14 个用例通过。
- **场景 (Scenario)**：部署环境变量拼写错误；修改日志文件命名或按 identity 筛选时。
- **关键词 (Keywords)**：CCAE Reporter, CCAE_REPORTER_LOG_LEVEL, warning, identity, rotation
