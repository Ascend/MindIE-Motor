# [2026-11-25] TUI 内部署触发 NodePort 冲突时按键无响应、界面卡死

- **现象 (Symptom)**：交互式 TUI 里按 `R` 部署，若生成 YAML 的 NodePort 已被占用，屏幕停在
  `[NodePort] 31015 (Coordinator infer) -> y / <port> / N:`；按键不回显，按 Enter 不生效，
  整个 TUI 挂死（只能 Ctrl-C / 杀进程）。CLI 方式部署的同一提示正常。
- **根因 (Root cause)**：TUI 用 `TerminalContext` 把终端切到 raw 模式并进入备用屏
  （`examples/deployer/lib/tui/terminal.py` 的 `tty.setraw`），而 `_prompt_one_conflict`
  用 builtin `input()`（`examples/deployer/lib/nodeport_allocator.py`）读取答案。raw 模式关闭
  ICANON/ECHO/ICRNL：Enter 只产生 CR (`\r`)，`input()` 的底层读行只以 LF (`\n`) 结束，也不回显，
  于是永远阻塞——pty 实测旧代码写 `y\r` 无返回，写 `y\n` 才返回。
- **为什么会写出 (Why)**：`input()` 被当成"任何终端都可用"的交互原语，只在 CLI（cooked 模式）
  验证过；TUI 侧也没有把"部署链路内部的交互提示"纳入设计，双方各自成立、组合即死。
- **修复 (Fix)**：`nodeport_allocator` 增加可插拔读取器 `set_conflict_prompt_reader(reader)`
  （默认仍走 `input()`，非 TTY 仍按 `N`）；TUI `_deploy_service` 在调用
  `deploy_services` 前安装 `_prompt_nodeport_conflict`（复用 `_read_line` raw 模式行编辑器，
  提示显示在状态行，读完恢复 `⏳ Deploying...`），`finally` 清除；读取器返回 `None`
  （Esc / 空行）与 `N` 等价。读取器存在时跳过非 TTY 判定（有 UI 才装）。读取前先
  `flush_input()` 丢弃部署等待期间滞留的按键（否则会被当成答案静默改写端口）；
  Ctrl-C 经 `_read_line(ctrl_c_aborts=True)` 抛 `DeployAborted`，中止本次部署并回到 TUI 菜单，
  与 CLI 的 Ctrl-C 中止对齐。
- **测试拦截 (Test interception)**：`tests/examples/deployer/test_nodeport_allocator.py`
  新增 4 例：读取器取代 `builtin.input`（旧代码直接 ImportError）、非 TTY 也走读取器、
  取消与非法输入重问、读取器抛异常时中止且不回写 yaml；
  `tests/examples/deployer/test_tui_deploy_nodeport.py` 锁定 TUI 部署期间安装/清理读取器、
  答案经 `_read_line` 读取、读取前 flush、Ctrl-C 中止部署、无终端时取消。
- **场景 (Scenario)**：TUI 中 `R` 部署，且生成 YAML 的 NodePort 被其它 namespace / 服务占用
  （同 namespace 自有端口不算冲突），首次部署或换 namespace 时最常见。
- **关键词 (Keywords)**：TUI, NodePort, input(), raw mode, setraw, 卡死
