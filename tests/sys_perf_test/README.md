# MindIE-Motor 防护网

提供易用的服务化性能与精度防护方案，该方案基于终端复用工具tmux

本工具的使用场景：
开发人员在合入代码前，需要先运行本测试工具。如果性能相较当前仓库有所下降，应当分析性能下降原因并修复问题后再合入代码。

## 依赖安装

### Ubuntu/Debian安装依赖

```bash
sudo apt install tmux
```

### CentOS/RHEL安装依赖

```bash
sudo yum install tmux
```

## 单独编译

若需要编译MindIE_Motor源码，则执行命令

```python
python3 compile_utils.py
```

该脚本实现了拉取编译三方依赖、编译三方依赖、完整编译MindIE-Motor仓的功能。编译的输出在output文件夹中。

该脚本的前置条件为用户准备好了MindIE编译镜像（包含完整配套的依赖）以及MindIE-LLM的配套run包。用户通过编辑config.json中的"compile_environment"字段指定编译镜像的名称以及MindIE-LLM的配套run包的路径。

## 单独执行测试用例

若需要测试已编译的MindIE_Motor，则可以直接执行全量防护网用例命令

```python
python3 sys_test.py
```

用户可以通过config.json来设置已有用例或新增自有用例。必填项为："Http"、"ManagementHttp"、"DatasetPath"、"ModelPath"、"config_path"

### 执行精度和性能测试用例

默认执行sys_test.py会全量运行精度和性能测试用例

若仅执行性能用例，则指定精度测试选项-a为false

```python
python3 sys_test.py -a false
```

若仅执行精度用例，则指定精度测试选项-p为false

```python
python3 sys_test.py -p false
```

## 编译并执行测试用例

执行一个脚本，实现先编译源码再运行全量用例

```python
python3 sys_test.py -c
```

config的配置参照单独编译和执行测试用例的配置要求
