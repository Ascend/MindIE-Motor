# MindIE-Motor DT

## 使用方法

1. **克隆项目**并在**MindIE-Motor项目根目录**下
2. **运行脚本**：

   ```bash
   bash tests/run_all_tests.sh --component [benchmark|mindieclient|all]  [--report-path REPORT_PATH] [--subdir SUBDIR]
   ```

### 参数说明

- `--component`  
  指定要测试的组件。可选值：`benchmark`、`mindieclient` 或 `all`（默认是 `all`）。

- `--report-path`  
  指定报告的保存路径（可选，默认为./tests目录下的 `reports` 文件夹）。

- `--subdir`  
  指定测试某个组件下的子目录/文件（可选）。

### 示例

- 运行所有测试：

   ```bash
  bash tests/run_all_tests.sh
   ```

- 仅运行 `benchmark` 测试并指定报告路径：

   ```bash
  bash tests/run_all_tests.sh --component benchmark --report-path ./my_reports
   ```

- 仅运行 `mindieclient` 测试只运行test_input_requested_output.py，指定报告路径：

   ```bash
   bash tests/run_all_tests.sh --component mindieclient  --subdir tests/mindie_client/test_input_requested_output.py
   ```

## 脚本功能

- **安装虚拟环境**：首次运行时自动创建并激活虚拟环境，安装依赖。
- **运行指定组件的测试**：根据用户输入的参数执行对应的测试脚本。
- **生成测试报告**：删除旧的报告文件夹，并生成测试报告，包括行覆盖率和分支覆盖率。

## 注意事项

- 确保 `install_env.sh` 脚本中的依赖项已正确配置。
- 在运行测试前，检查相应的测试脚本是否存在于指定路径。
