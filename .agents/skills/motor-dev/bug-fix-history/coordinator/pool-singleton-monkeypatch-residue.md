# [2026-11-14] 跨文件用例闪挂：HTTPClientPool 单例残留实例级 monkeypatch

- **现象**：`tests/coordinator/api_server/test_count_tokens.py` 单文件 17/17 全绿；与 `tests/coordinator/router/test_native_pd_contract.py` 同一 pytest 会话（`--serial` 同序、或 xdist 同 worker）时 11 个用例拿到 502/504 而非 mock 的 404/429/500，日志出现真实的 `HTTPClientPool new client created`。
- **根因**：`test_native_pd_contract.py` 的 `test_native_pd_client_uses_coordinator_inference_tls_config` 对单例**实例**执行 `monkeypatch.setattr(pool, "get_client", ...)`；monkeypatch teardown 把 bound method 写回实例 `__dict__`，永久遮蔽后续任何对 `HTTPClientPool.get_client` 的**类级** patch。受害文件的类级 patch 静默失效后，真实 `get_client` 去连 `127.0.0.1:8000`，ConnectError 被 `render_transport_error` 映射为 502。
- **为什么会写出**：误以为 monkeypatch 对实例属性是无痕撤销——对「属性本来定义在类上」的实例做 setattr，undo 会把绑定方法留在实例字典。定位时还第一反应归因于 teardown 的 `close_all()`（最小化对照实验证明其余 3 条带 `close_all` 的用例不污染），先做单用例级二分再下结论才锁定真凶。
- **修复**：污染侧（tls 用例）改为类级 `monkeypatch.setattr(HTTPClientPool, "get_client", ...)`；受害侧（count_tokens 夹具）改为 patch `count_tokens` 模块解析的 `HTTPClientPool` 名字（`lambda: SimpleNamespace(get_client=...)`），与全局单例状态彻底解耦，任何文件对单例做什么都不再影响本套组。
- **测试拦截**：tls 用例新增 `assert "get_client" not in HTTPClientPool().__dict__`（旧代码实例字典含 mock，必失败；新代码通过）；两种文件顺序 `run_tests.sh --serial` 混跑 + 全量 `tests/coordinator/` 全绿。
- **场景**：任何对 `ThreadSafeSingleton` 实例做 `monkeypatch.setattr` 的测试，与后续类级 patch 同一属性的测试落入同一 pytest 进程（`run_tests.sh` 默认 `-n 6` 时同 worker）。
- **关键词**：HTTPClientPool, monkeypatch, singleton 残留, 测试隔离, xdist 闪挂。
