# engine_config 命令行转换工具

## 概述

PyMotor 的 `user_config.json` 中，`motor_engine_prefill_config` / `motor_engine_decode_config` 下的 **`engine_config`** 字段与推理引擎启动命令（如 vLLM 的 `vllm serve`、SGLang 的 `python -m sglang.launch_server`）的 CLI 参数等价——去掉 `--` 前缀后以 JSON 键值写入即可。

若已有可运行的引擎启动命令，可使用本文提供的转换脚本，将 `serve` 后的 CLI 参数批量转为 `engine_config` JSON 对象，再粘贴到配置文件中。

## 适用场景

- 从单机调试命令迁移到 `user_config.json` 的 `engine_config`
- 批量转换多个 `--key value` 参数，避免手工逐条映射

脚本仅做 CLI → JSON 的结构转换，**不包含**引擎依赖；运行脚本仅需 Python 3 标准库。

## 用法

1. 将 [附录](#附录脚本源码) 中的 Python 代码保存为 `cli_to_engine_config.py`。
2. 传入 `serve` 后面的参数（前面加 `--` 与脚本自身参数分隔）：

   ```bash
   python cli_to_engine_config.py -- /mnt/weight/qwen3_8B \
     --served-model-name qwen3-8B --tensor-parallel-size 2 --max-model-len 2048
   ```

3. 将 stdout 输出的 JSON 粘贴到 `motor_engine_prefill_config` / `motor_engine_decode_config` 的 `engine_config` 中。

**示例输出**：

```json
{
  "model": "/mnt/weight/qwen3_8B",
  "served-model-name": "qwen3-8B",
  "tensor-parallel-size": 2,
  "max-model-len": 2048
}
```

## 转换规则

| 输入 | 输出 |
|------|------|
| `--key value` | `"key": value` |
| `--flag`（无值） | `"flag": true` |
| 首个非 `--` 参数 | `"model": "..."` |
| `--kv-transfer-config.kv_connector xxx` | 嵌套为 `kv_transfer_config.kv_connector` |
| 值以 `{` / `[` 开头 | 按 JSON 对象或数组解析 |

`bool`、`int`、`float` 会自动识别类型。连字符 `-` 与下划线 `_` 两种键名形式均保留原样（与引擎官方文档保持一致）。

<a id="附录脚本源码"></a>

## 附录：参考脚本

将以下代码保存为 `cli_to_engine_config.py` 后使用：

```python
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import argparse
import json


def _coerce(raw: str):
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if raw[:1] in "{[":
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    for caster in (int, float):
        try:
            return caster(raw)
        except ValueError:
            pass
    return raw


def _set_config(config: dict, key: str, value) -> None:
    """--key value -> config；--a.b.c value -> 嵌套对象。"""
    if "." not in key:
        config[key] = value
        return
    node = config
    for part in key.split(".")[:-1]:
        name = part.replace("-", "_")
        if not isinstance(node.get(name), dict):
            node[name] = {}
        node = node[name]
    node[key.rsplit(".", 1)[-1].replace("-", "_")] = value


def cli_to_config(tokens: list) -> dict:
    """--key value / --flag / 首个位置参数(model) -> dict。"""
    config: dict = {}
    rest = list(tokens)
    while rest:
        head, *rest = rest
        if not head.startswith("--"):
            config.setdefault("model", head)
            continue
        key = head[2:]
        if rest and not rest[0].startswith("--"):
            _set_config(config, key, _coerce(rest[0]))
            rest = rest[1:]
        else:
            _set_config(config, key, True)
    return config


def main() -> None:
    parser = argparse.ArgumentParser(
        description="将 serve 后的 CLI 参数转为 engine_config JSON",
        epilog="示例: %(prog)s -- /path/to/model --tensor-parallel-size 2",
    )
    parser.add_argument(
        "cli_args",
        nargs=argparse.REMAINDER,
        help="serve 后面的参数（前面加 --）",
    )
    args = parser.parse_args()
    tokens = args.cli_args
    if tokens[:1] == ["--"]:
        tokens = tokens[1:]
    if not tokens:
        parser.error("缺少参数，示例: cli_to_engine_config.py -- /path/to/model --tensor-parallel-size 2")
    print(json.dumps(cli_to_config(tokens), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
```
