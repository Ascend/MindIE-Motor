# API KEY的生成及使用

## 使用 `generate_api_key.py` 生成和使用 API Key

本目录下的 `generate_api_key.py` 是用于为 MindIE Motor 生成 API Key 的工具脚本，脚本内部已经接入了 `motor.common.http.key_encryption` 中的加密实现。

### 运行方式

建议在项目根目录（包含 `examples/`、`motor/` 的目录）下执行：

```bash
python examples/api_key/generate_api_key.py \
  [--key <plain_api_key>] \
  [--algorithm <name>] \
  [--iterations <N>]
```

其中：

- **`--key`**：明文 API Key，可选。
  - 不传时脚本会自动生成形如 `sk-xxxxx` 的随机 Key，并在终端打印出来。
- **`--algorithm`**：加密算法名称，可选。
  - 默认值：`PBKDF2_SHA256`。
  - 可选值列表来自 `motor.common.http.key_encryption.get_supported_algorithms()`，当前内置为 `["PBKDF2_SHA256"]`。
- **`--iterations`**：迭代次数，可选，仅在算法为 `PBKDF2_SHA256` 时生效。
  - 默认值：`100000`，必须为正整数。
  - 生成的密文中会包含该迭代次数（格式为 `salt:iterations:hash`），校验时会自动解析并使用同样的迭代次数。

### 常见示例

- **自动生成随机 API Key：**

```bash
python examples/api_key/generate_api_key.py
```

- **使用指定明文 Key：**

```bash
python examples/api_key/generate_api_key.py --key "sk-test123456789"
```

- **指定 PBKDF2 迭代次数：**

```bash
python examples/api_key/generate_api_key.py --iterations 200000
```

执行完成后，脚本会打印两部分关键信息：

- **Plain API Key**：客户端调用接口时需要携带的明文 Key。
- **Encrypted API Key**：需要写入服务端配置的加密 Key。

### 在配置中使用生成的 API Key

脚本会给出一个类似下面的配置示例（以 `examples/infer_engines/vllm/user_config.json` 为例），你可以按需合并到自己的配置中：

```json
{
  "motor_coordinator_config": {
    "api_key_config": {
      "enable_api_key": true,
      "valid_keys": ["<encrypted_key_here>"],
      "encryption_algorithm": "PBKDF2_SHA256",
      "header_name": "Authorization",
      "key_prefix": "Bearer "
    }
  }
}
```

- **`enable_api_key`**：开启 / 关闭 API Key 校验。
- **`valid_keys`**：一组加密后的合法 API Key（脚本输出的 Encrypted API Key 放到这里）。
- **`encryption_algorithm`**：与生成时使用的算法保持一致，例如 `PBKDF2_SHA256`。
- **`header_name` / `key_prefix`**：服务端从 HTTP 请求中读取 API Key 时使用的 Header 名称和前缀，具体取值以工程内现有配置为准（常见写法为 `Authorization: Bearer <plain_key>`）。

**注意**：
如果开启了 `enable_api_key = true`，但未配置或留空 `valid_keys`，协调器配置校验会认为配置不合法并抛出异常，从而避免“开启了校验却没有有效 Key”的错误配置。

客户端侧只需要在请求中携带明文 Key，例如：

```http
Authorization: Bearer sk-test123456789
```

服务端会自动根据配置的 `valid_keys` 和加密算法进行校验。

## 自定义 API Key 加密方式

默认情况下，系统内置的算法为 `PBKDF2_SHA256`，实现位于 `motor/common/http/key_encryption.py` 中的 `PBKDF2KeyEncryption`。如果你希望引入自己的加密方案（例如接入硬件安全模块或公司统一密码库），可以按以下步骤扩展。

### 实现自定义加密类

在合适的位置（推荐在 `motor/common/http/key_encryption.py` 或单独模块中），实现一个继承自 `KeyEncryptionBase` 的类，例如：

```python
from motor.common.http.key_encryption import KeyEncryptionBase


class MyCustomKeyEncryption(KeyEncryptionBase):
    @classmethod
    def encrypt_key(cls, plain_key: str) -> str:
        # TODO: 返回你自定义格式的密文（需要包含校验所需的全部信息）
        ...

    def verify_key(self, plain_key: str, encrypted_key: str) -> bool:
        # TODO: 根据加密时的格式解析 encrypted_key，并判断是否匹配
        ...

    def get_algorithm_name(self) -> str:
        return "MY_CUSTOM_ALGO"
```

实现时建议遵循以下约定：

- **密文中包含所有校验所需参数**（例如 salt、迭代次数、版本号等），避免依赖额外的外部状态。
- **`encrypt_key` 与 `verify_key` 要保持兼容**：用同一实现加密出来的密文，必须能被 `verify_key` 正确校验。

### 注册自定义算法

`key_encryption.py` 中有一个内置算法映射和注册逻辑：

- 内置算法字典：`_builtin_algorithms = {"PBKDF2_SHA256": PBKDF2KeyEncryption}`
- 注册方法：`register_encryption_algorithm(name, algorithm_class)`
- 根据配置名注册：`register_algorithm_from_config(algorithm_name)`

如果你要让配置和 `generate_api_key.py` 都识别你的算法，最简单的方式是在 `_builtin_algorithms` 中加入一项：

```python
from motor.common.http.key_encryption import (
    PBKDF2KeyEncryption,
    KeyEncryptionBase,
)

_builtin_algorithms = {
    "PBKDF2_SHA256": PBKDF2KeyEncryption,
    "MY_CUSTOM_ALGO": MyCustomKeyEncryption,  # 新增条目
}
```

这样会带来几个效果：

- `get_supported_algorithms()` 会包含 `"MY_CUSTOM_ALGO"`，`generate_api_key.py` 的 `--algorithm` 参数也会自动支持该值。
- 配置文件里可以写 `encryption_algorithm: "MY_CUSTOM_ALGO"`，启动时会通过 `register_algorithm_from_config` 完成注册。

### 使用自定义算法生成和校验 API Key

1. **生成密钥**

    ```bash
    python examples/api_key/generate_api_key.py \
      --key "sk-my-custom-key" \
      --algorithm "MY_CUSTOM_ALGO"
    ```

    脚本会调用你实现的 `MyCustomKeyEncryption.encrypt_key`，输出对应的密文。

2. **写入配置**

    在配置中将 `encryption_algorithm` 设置为 `"MY_CUSTOM_ALGO"`，并把脚本输出的加密 Key 写入 `valid_keys`：

    ```json
    {
      "motor_coordinator_config": {
        "api_key_config": {
          "enable_api_key": true,
          "valid_keys": ["<my_custom_encrypted_key>"],
          "encryption_algorithm": "MY_CUSTOM_ALGO"
        }
      }
    }
    ```

3. **客户端调用**

客户端依然只需要携带明文 Key（如 `sk-my-custom-key`），服务端会自动根据 `encryption_algorithm` 选择你的自定义加密算法进行校验，无需修改调用方式。
