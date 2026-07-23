# MindIE Motor支持HTTPS加密通信

## 快速开始

按照以下步骤启用TLS加密通信：

> **重要提示**：
>
> - 以下操作需要在**宿主机**上执行，因为`/mnt`目录通过hostPath方式挂载到容器中
> - **在多节点集群环境中**，`/mnt`目录必须是**共享存储**（如NFS、CephFS等），确保所有节点都能访问相同的证书文件
> - 如果使用本地存储，需要确保每个节点都执行以下步骤，或者使用共享存储方案

1. 在宿主机上创建证书脚本目录。

   ```sh
   mkdir -p /mnt/cert_scripts # you can specific your cert scripts dir
   ```

2. 拷贝证书生成脚本到宿主机的/mnt/cert_scripts/目录。

   ```sh
   cp openssl_gen_ca.sh /mnt/cert_scripts/ # path could be specific
   cp openssl_gen_cert.sh /mnt/cert_scripts/ # path could be specific
   chmod +x /mnt/cert_scripts/*.sh
   ```

3. 在宿主机上生成CA证书。

   ```sh
   bash /mnt/cert_scripts/openssl_gen_ca.sh /mnt/cert_scripts/ca/ # path could be specific
   ```

4. 配置相关环境变量。

   在部署配置文件（如`env.json`）的`motor_common_env`中添加：

   ```json
   {
     "version": "2.0.0",
     "motor_common_env": {
       ...,
       "ENABLE_GEN_CERT": "true"
     },
     ...
   }
   ```

   **必需环境变量**：
   - `ENABLE_GEN_CERT`: 设置为`"true"`启用TLS证书自动生成

   **可选环境变量**（不配置则使用默认值）：
   - `CA_PATH`: CA证书所在路径（默认：`/mnt/cert_scripts/ca`）
   - `BASE_CERT_PATH`: 证书保存的基础路径（默认：`/usr/local/Ascend/pyMotor/conf/security`）
   - `CERT_NAMES`: 需要生成的证书名称列表（默认：`infer mgmt`）
     - `etcd`和`grpc`证书为可选，仅在启用ETCD相关功能（持久化或主备）且对ETCD有安全性要求时才需要配置
   - `GEN_CERT_SCRIPT`: 证书生成脚本路径（默认：`/mnt/cert_scripts/openssl_gen_cert.sh`）

5. 配置证书路径。

   在`user_config.json`中配置TLS证书路径，详情请参见 [配置证书](#配置证书)。

6. 启动服务，系统会自动生成所需证书。

## 详细说明

### 准备证书生成环境

**前提条件**：

- YAML配置中已通过hostPath方式挂载宿主机的`/mnt`目录到容器。
- 需要在宿主机上准备证书生成脚本和CA证书。

**在宿主机上执行**：

```sh
# 创建证书脚本目录
mkdir -p /mnt/cert_scripts

# 拷贝脚本（假设脚本在当前目录）
cp openssl_gen_ca.sh /mnt/cert_scripts/
cp openssl_gen_cert.sh /mnt/cert_scripts/
chmod +x /mnt/cert_scripts/*.sh

# 生成CA证书
bash /mnt/cert_scripts/openssl_gen_ca.sh /mnt/cert_scripts/ca/
```

### 生成服务端证书

通过`openssl_gen_cert.sh`生成服务端证书，将其拷贝到`/mnt/cert_scripts`目录下。

证书生成逻辑已集成到`common.sh`中，会自动根据环境变量`ENABLE_GEN_CERT`判断是否生成证书。

#### 配置ENABLE_GEN_CERT环境变量

在部署配置文件（如`env.json`）中添加`ENABLE_GEN_CERT`环境变量：

```json
{
  "version": "2.0.0",
  "motor_common_env": {
    ...,
    "ENABLE_GEN_CERT": "true"
  },
  ...
}
```

**说明：**

- 在`motor_common_env`中添加`"ENABLE_GEN_CERT": "true"`即可启用TLS证书自动生成。
- 当`ENABLE_GEN_CERT`设置为`true`时，`common.sh`会在服务启动时自动调用证书生成函数。
- 证书会生成到`/usr/local/Ascend/pyMotor/conf/security/`目录下。
- 生成的证书包括：infer、mgmt、etcd、grpc四种类型。

#### 自定义证书路径配置（可选）

如果需要自定义证书路径，可通过以下环境变量配置：

```json
{
  "version": "2.0.0",
  "motor_common_env": {
    ...,
    "ENABLE_GEN_CERT": "true",
    "CA_PATH": "/mnt/cert_scripts/ca",
    "BASE_CERT_PATH": "/usr/local/Ascend/pyMotor/conf/security",
    "CERT_NAMES": "infer mgmt",
    "GEN_CERT_SCRIPT": "/mnt/cert_scripts/openssl_gen_cert.sh"
  },
  ...
}
```

**环境变量说明：**

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `CA_PATH` | CA证书所在路径 | `/mnt/cert_scripts/ca` |
| `BASE_CERT_PATH` | 证书保存的基础路径 | `/usr/local/Ascend/pyMotor/conf/security` |
| `CERT_NAMES` | 需要生成的证书名称列表，空格分隔 | `infer mgmt` |
| `GEN_CERT_SCRIPT` | 证书生成脚本路径 | `/mnt/cert_scripts/openssl_gen_cert.sh` |

**注意**：`CERT_NAMES`默认只包含`infer`和`mgmt`证书。`etcd`和`grpc`证书为可选，仅在启用ETCD相关功能（持久化或主备）且对ETCD有安全性要求时才需要添加。

**使用场景：**

- 自定义CA证书存储位置。
- 修改证书生成目录。
- 调整需要生成的证书类型。
- 使用非标准路径的证书生成脚本。

**生成的文件：**
对于每个cert_name，会在`${BASE_CERT_PATH}/${cert_name}/`目录下生成：

- `cert.pem` - 服务器证书
- `cert.key.pem` - 加密的私钥
- `decrypt.cert.key.pem` - 未加密的私钥（用于配置中的key_file）
- `cert.conf` - OpenSSL配置文件

### 配置证书

在`user_config.json`中增加如下配置项，`enable`设置成`true`，同时证书路径要配置正确。

> 限制：当前只支持解密的`key_file`, `passwd_file`和`tls_crl`为预留字段。

```json
{
  "motor_deploy_config": {
    "tls_config": {
      "infer_tls_config": {
        "enable_tls": true,
        "ca_file": "/usr/local/Ascend/pyMotor/conf/security/infer/ca.pem",
        "cert_file": "/usr/local/Ascend/pyMotor/conf/security/infer/cert.pem",
        "key_file": "/usr/local/Ascend/pyMotor/conf/security/infer/decrypt.cert.key.pem",
        "passwd_file": "",
        "tls_crl": ""
      },
      "mgmt_tls_config": {
        "enable_tls": true,
        "ca_file": "/usr/local/Ascend/pyMotor/conf/security/mgmt/ca.pem",
        "cert_file": "/usr/local/Ascend/pyMotor/conf/security/mgmt/cert.pem",
        "key_file": "/usr/local/Ascend/pyMotor/conf/security/mgmt/decrypt.cert.key.pem",
        "passwd_file": "",
        "tls_crl": ""
      },
      "etcd_tls_config": {
        "enable_tls": false,
        "ca_file": "/usr/local/Ascend/pyMotor/conf/security/etcd/ca.pem",
        "cert_file": "/usr/local/Ascend/pyMotor/conf/security/etcd/cert.pem",
        "key_file": "/usr/local/Ascend/pyMotor/conf/security/etcd/decrypt.cert.key.pem",
        "passwd_file": "",
        "tls_crl": ""
      },
      "grpc_tls_config": {
        "enable_tls": false,
        "ca_file": "/usr/local/Ascend/pyMotor/conf/security/grpc/ca.pem",
        "cert_file": "/usr/local/Ascend/pyMotor/conf/security/grpc/cert.pem",
        "key_file": "/usr/local/Ascend/pyMotor/conf/security/grpc/decrypt.cert.key.pem",
        "passwd_file": "",
        "tls_crl": ""
      }
    }
  }
}
```

**说明**：

- `infer_tls_config`和`mgmt_tls_config`：默认启用TLS加密通信。
- `etcd_tls_config`和`grpc_tls_config`：默认禁用，仅在启用ETCD相关功能（持久化或主备）且对ETCD有安全性要求时才需要启用。
- 如需启用etcd和grpc的TLS，需要：
  1. 在`env.json`中设置`CERT_NAMES`环境变量包含`etcd grpc`（如：`"CERT_NAMES": "infer mgmt etcd grpc"`）。
  2. 将对应的`enable_tls`设置为`true`。

## 注意事项

- **重要**：证书生成脚本和CA证书必须在**宿主机**的`/mnt/cert_scripts/`目录中准备
- **多节点集群**：在多节点环境中，`/mnt`目录必须使用共享存储（如NFS、CephFS等），确保所有节点都能访问相同的证书文件
- **单节点或本地存储**：如果使用本地存储，需要在每个节点上重复执行证书准备工作
- **自定义路径**：可通过环境变量`CA_PATH`、`BASE_CERT_PATH`、`CERT_NAMES`、`GEN_CERT_SCRIPT`自定义证书路径
- YAML配置中通过hostPath方式挂载宿主机的`/mnt`目录，容器内可访问`/mnt/cert_scripts/`
- 证书生成应在服务启动之前完成，确保服务启动时证书文件已存在
- 如果证书已存在，脚本会提示是否覆盖，可根据实际需求选择
- 证书路径和名称需要与`user_config.json`中的配置保持一致
- 证书生成逻辑已集成到`common.sh`中，无需手动修改`boot.sh`
- 如果启动时报错找不到脚本或证书，请检查宿主机的`/mnt/cert_scripts/`目录是否正确配置，或检查环境变量配置
