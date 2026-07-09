# secure_h2d功能使用指导

## 一、约束

1.本功能解决的核心问题是host和device侧的数据传输安全问题，host内各进程之间的安全不在解决范围内，默认主机内的进程都处在信任域内，密钥是通过socket文件的访问控制做的，host内部可以访问到socket文件的进程，都能拿到密钥。

2.本功能不支持模型并行，默认一张卡不能被多个模型使用

3.本功能仅支持docker模式拉起的推理服务

4.本功能目前仅适配特定版本的HDK和vllm-ascend版本(详见README)

## 二、secure_patch 环境部署说明

部署文档详情请参考：[secure_h2d功能部署](README.md)

## 三、功能使用

进入指定容器后，选择对应的配置进行source

```bash
#示例
source env_aes_ctr_a2.sh
```

然后用正常方式拉起推理服务即可

## 四、secure_patch 环境变量说明

### 1. 基础开关与运行模式

| 环境变量                      | 示例值 | 含义                                                         |
| ----------------------------- | ------ | ------------------------------------------------------------ |
| `SECURE_PATCH_DEVICE_ID_MODE` | `A3`   | 指定 device_id 映射模式。用于适配不同硬件/部署形态下 PyTorch 侧 device_id 与 KMS/算子侧 device_id 的映射关系。`A3` 表示按 A3 场景的设备编号规则处理。（默认是A2） |
| `SECURE_PATCH_ENABLE`         | `1`    | 是否启用 secure_patch。`1` 表示启用，`0` 表示关闭。关闭后不安装 H2D/D2H/权重加载相关 patch。 |
| `SECURE_PATCH_DEBUG`          | `0`    | 是否开启调试日志。`1` 输出更详细日志，便于联调；`0` 关闭调试日志，适合性能测试或生产运行。 |

### 2. KMSAgent 通信配置

| 环境变量                              | 示例值                               | 含义                                                         |
| ------------------------------------- | ------------------------------------ | ------------------------------------------------------------ |
| `SECURE_PATCH_KMS_SOCKET`             | `/run/kmsagent/socket/kmsagent.sock` | KMSAgent 的 UDS socket 路径。PyTorch 侧通过该路径向 KMSAgent 请求初始密钥和更新密钥。 |
| `SECURE_PATCH_KMS_TIMEOUT`            | `10.0`                               | KMS 请求的总超时时间，单位为秒。用于限制一次密钥请求/更新请求的最大等待时间。 |
| `SECURE_PATCH_KMS_RETRY_MAX`          | `3`                                  | KMS 请求失败后的最大重试次数。这里表示最多重试 3 次。        |
| `SECURE_PATCH_KMS_RETRY_WAIT_MS`      | `3000`                               | 每次重试前的等待时间，单位为毫秒。这里表示每次失败后等待 3 秒再重试。 |
| `SECURE_PATCH_KMS_RETRY_BACKOFF`      | `1.0`                                | 重试退避系数。`1.0` 表示固定间隔重试；如果设置为大于 1 的值，则每次重试等待时间按比例增加。 |
| `SECURE_PATCH_KMS_CONNECT_TIMEOUT_MS` | `200`                                | 连接 KMSAgent UDS socket 的超时时间，单位为毫秒。            |
| `SECURE_PATCH_KMS_RECV_TIMEOUT_MS`    | `500`                                | 等待 KMSAgent 响应数据的接收超时时间，单位为毫秒。           |

### 3. 算法与 IV 配置

| 环境变量                         | 示例值             | 含义                                                         |
| -------------------------------- | ------------------ | ------------------------------------------------------------ |
| `SECURE_PATCH_ALG_ID`            | `1`                | 加解密算法 ID。 `1` 表示 AES-CTR-128  `2` 表示 AES-GCM-128   |
| `SECURE_PATCH_IV_BYTES`          | `16`               | IV 长度，单位为字节。AES-CTR 场景通常使用 16 字节 IV。H2D 和 D2H 方向会分别维护 IV counter，避免 IV 复用。 |
| `SECURE_PATCH_HOST_CTR_MODULE`   | `aes_ctr_crypt`    | Host 侧 CTR 加密模块名。secure_patch 会从该模块加载 Host 侧加解密函数。 |
| `SECURE_PATCH_HOST_CTR_FUNCTION` | `aes_ctr_cryption` | Host 侧 CTR 加解密函数名。H2D 场景用于 Host 加密，D2H 场景用于 Host 解密。 |

### 4. 密钥轮换配置

| 环境变量                             | 示例值        | 含义                                                         |
| ------------------------------------ | ------------- | ------------------------------------------------------------ |
| `SECURE_PATCH_ROTATE_BYTES`          | `34359738368` | 按数据量触发密钥轮换的阈值，单位为字节。该值为 32GB，表示单方向累计处理数据量达到阈值后触发密钥更新逻辑。 |
| `SECURE_PATCH_ROTATE_OPS`            | `100000000`   | 按加解密次数触发密钥轮换的阈值。这里表示单方向累计加解密操作达到 1 亿次后触发密钥更新逻辑。 |
| `SECURE_PATCH_ROTATE_PREFETCH_RATIO` | `0.8`         | 密钥预取比例。表示当使用量达到轮换阈值的 80% 时，提前异步请求下一组密钥，降低真正轮换时的同步等待开销。 |
| `SECURE_PATCH_ROTATE_ALLOW_STALE`    | `1`           | 是否允许在新密钥尚未准备好时继续使用当前旧密钥。`1` 表示允许短暂使用当前密钥，避免业务阻塞；`0` 表示轮换点必须等待新密钥就绪。 |

### 5. 密钥缓存与 fallback 配置

| 环境变量                                    | 示例值 | 含义                                                         |
| ------------------------------------------- | ------ | ------------------------------------------------------------ |
| `SECURE_PATCH_MAX_KEYS_PER_DIRECTION`       | `2`    | 每个方向最多缓存的密钥数量。这里表示 H2D 和 D2H 每个方向各保留新旧两把密钥，用于密钥切换窗口内的兼容和回退。 |
| `SECURE_PATCH_KEY_FALLBACK_ENABLE`          | `1`    | 是否启用密钥 fallback。`1` 表示当最新密钥加解密失败时，可以尝试同方向缓存中的旧密钥。 |
| `SECURE_PATCH_KEY_FAILURE_DEMOTE_THRESHOLD` | `3`    | 密钥失败降级阈值。某把密钥连续失败达到该次数后，可将其降级或从优先路径中移除，避免反复使用异常密钥。 |

### 6. vLLM Patch 控制

| 环境变量                              | 示例值 | 含义                                                         |
| ------------------------------------- | ------ | ------------------------------------------------------------ |
| `SECURE_PATCH_PATCH_VLLM_COPY_TO_GPU` | `1`    | 是否 patch vLLM 的  token H2D 数据拷贝路径。`1` 表示对 CPU 到 NPU 的 token数据路径执行 Host 加密、Device 解密。 |
| `SECURE_PATCH_PATCH_ASYNC_OUTPUT_D2H` | `1`    | 是否 patch vLLM 的 D2H 输出路径。`1` 表示对 NPU 到 CPU 的token输出数据路径执行 Device 加密、Host 解密。 |
| `SECURE_PATCH_PATCH_WEIGHT_LOADER`    | `0`    | 是否 patch 权重加载路径。`0` 表示当前不对权重 H2D 加载路径加密；`1` 表示权重加载时也走 secure_patch 加解密流程。 |

### 7. KMS 异步预取配置

| 环境变量                            | 示例值 | 含义                                                         |
| ----------------------------------- | ------ | ------------------------------------------------------------ |
| `SECURE_PATCH_KMS_ASYNC_ENABLE`     | `1`    | 是否启用 KMS 异步请求能力。`1` 表示密钥预取和部分更新请求可以异步执行，减少业务主路径阻塞。 |
| `SECURE_PATCH_KMS_ASYNC_QUEUE_SIZE` | `128`  | KMS 异步请求队列大小。用于限制最多排队的异步密钥请求数量，避免异常情况下无限堆积。 |
