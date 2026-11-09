# [2026-11-05] 版本补丁脚本静默跳过安装目录

- **现象 (Symptom)**：`patch/0.30.0/apply_patches.sh` 在其他 Git 仓库内部的 site-packages 上报告成功，但目标文件未改变；补丁冲突也可能未被识别。
- **根因 (Root cause)**：`git -C <安装目录> apply` 向上发现父仓库后，按仓库子目录过滤补丁路径，可能静默跳过全部文件。普通非仓库安装目录可正常使用 `git apply`，缺少 `.git` 本身不是错误。
- **为什么会写出 (Why)**：仅检查命令退出码，未覆盖安装目录位于无关仓库内部的场景。
- **修复 (Fix)**：所有预检、反向检查和实际应用统一通过 helper 调用，将 `GIT_CEILING_DIRECTORIES` 设置为安装目录的父目录，阻止父仓库发现，同时允许安装目录本身为 editable Git 仓库。
- **测试拦截 (Test interception)**：`tests/examples/deployer/test_apply_package_patches.py` 使用真实 `git apply` 和模拟 pip 元数据，检查普通安装、父仓库内安装、editable 安装的实际文件变化、重复应用及第二个包冲突时第一个包不变；旧脚本在父仓库场景失败。
- **场景 (Scenario)**：在源码仓库下创建虚拟环境，或将包安装到仓库内的自定义目录。
- **关键词 (Keywords)**：apply_patches, git apply, site-packages, GIT_CEILING_DIRECTORIES, silent skip
