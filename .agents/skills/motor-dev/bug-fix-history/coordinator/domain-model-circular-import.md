# [2026-08-24] Coordinator models and domain package formed a circular import

- **现象 (Symptom)**：全新 Python 进程先导入 `management_server` 或 `models.request` 时，可能报错无法从“partially initialized module”导入 `RequestInfo`；测试通过调整导入顺序后可暂时隐藏问题。
- **根因 (Root cause)**：`models.request` 导入 `domain.scheduling_constraint` 时会先执行 `domain/__init__.py`，后者又提前导入 `workload_calculator`、`request_manager` 和 `scheduling`，这些模块反向导入尚未初始化完成的 `models.request`。
- **为什么会写出 (Why)**：包级聚合导出被当作无副作用的便利入口，但没有考虑 Python 在加载任意子模块前都会先完整执行包初始化文件。
- **修复 (Fix)**：`domain/__init__.py` 保留原公开符号，通过模块 `__getattr__` 按需导入并缓存，不再在包初始化阶段加载依赖 Coordinator models 的实现模块；恢复测试的自然导入顺序。
- **测试拦截 (Test interception)**：`test_package_imports.py` 在全新解释器中先导入 `models.request`，再访问 domain 包公开导出，确保模型优先加载和兼容导出同时可用。
- **场景 (Scenario)**：新进程、独立测试收集或应用入口先加载 Coordinator models，再加载 domain 公开接口时。
- **关键词 (Keywords)**：Coordinator, circular import, domain __init__, models.request, lazy exports
