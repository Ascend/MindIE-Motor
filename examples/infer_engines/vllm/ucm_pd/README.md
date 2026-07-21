# 分布式 PD + UCM 前缀缓存（内联配置 + 动态 PVC）

本示例演示在分布式 PD 之上把 **UCMConnector** 作为存储层接入，做 Prefill 节点的前缀缓存复用。

## 拓扑

- **Prefill** = `MultiConnector[ MooncakeConnectorV1(kv_producer), UCMConnector(kv_both) ]`
  - `connectors[0]` Mooncake 负责把 KV 从 P 实时传到 D（沿用现有能力）。
  - `connectors[1]` UCM 做前缀缓存池，配置**内联**在 `kv_connector_extra_config` 里，不使用 `UCM_CONFIG_FILE`、不挂 yaml 文件。
- **Decode** = 裸 `MooncakeConnectorV1(kv_consumer)`，不带 UCM。

## 关键点

- UCM 连接器恒 `kv_role: "kv_both"`（双向 store）；PyMotor 不会向其 `kv_connector_extra_config` 注入任何端口键。
- `kv_connector_module_path` 必填，vLLM 据此动态加载 UCMConnector。
- 镜像用 UCM 叠加镜像 `mindie-motor-vllm-ucm:3.0.0b2`（基于 vllm-ascend v0.20.2rc1，见 `docker/mindie-motor-vllm-ucm/`）。

## 存储（通用列表:动态 PVC / 直挂 NFS + 独立 /dev/shm）

存储是**通用能力**,不是 UCM 专属:`motor_deploy_config.storage` 是一个**列表**,每个条目挂一块卷进所有引擎 pod,可挂多块。每个条目**必须显式声明 `type`**;卷名与**动态创建的** PVC 名由部署器按序号自动生成(`mindie-motor-store-<i>`),不可配置(挂已有 PVC 时用 `claim_name` 指定要挂的 claim,见下)。

- **`type: "pvc"`** —— 两种互斥模式:填 `storage_class_name` 动态创建新 PVC(需要集群有带 CSI/供给器的 StorageClass);或填 `claim_name` 直接挂载命名空间里**已有的 PVC**(不生成 PVC 对象,不能与 `storage_class_name`/`size`/`access_mode` 同填)。P/D 挂同一 claim 即共享。
- **`type: "nfs"`** —— k8s 原生 NFS 卷,直接挂 `server:path`,**不需要 PVC、StorageClass、供给器**,只要 NFS 导出可达、节点装了 nfs 客户端;各 pod 挂同一导出即天然共享。集群没有 CSI 时用这个。
- **`type: "hostpath"`** —— 节点本地目录直挂。**注意:hostPath 本身不跨节点共享**——只有当该路径在每个节点都预先挂了同一共享文件系统(如各节点同路径挂同一 NFS 导出)时,P/D 才真正共享;纯本地目录会静默造成跨节点前缀缓存零命中。能用 `nfs`/`pvc` 就别用它。

`pvc` 条目字段:

| 字段 | 必填 | 默认 | 含义 |
|---|---|---|---|
| `type` | 是 | — | `"pvc"` |
| `storage_class_name` | 二选一 | — | 动态创建新 PVC:支持动态供给的 StorageClass(跨节点共享需 RWX) |
| `claim_name` | 二选一 | — | 挂载已有 PVC:命名空间里已存在的 claim 名(与上互斥,且不能再填 `size`/`access_mode`) |
| `mount_path` | 建议 | `/mnt/store-<i>` | 挂载点,需与 UCM `storage_backends` 一致 |
| `access_mode` / `size` / `enable` | 否 | `ReadWriteMany` / `200Gi` / `true` | 仅动态创建模式有效 |

挂已有 PVC 写法示例:

```json
"storage": [
  { "type": "pvc", "claim_name": "my-shared-pvc", "mount_path": "/mnt/ucm" }
]
```

`nfs` 条目字段:

| 字段 | 必填 | 默认 | 含义 |
|---|---|---|---|
| `type` | 是 | — | `"nfs"` |
| `server` | 是 | — | NFS 服务器地址 |
| `path` | 是 | — | NFS 导出目录 |
| `mount_path` | 建议 | `/mnt/store-<i>` | 挂载点,需与 UCM `storage_backends` 一致 |
| `read_only` / `enable` | 否 | `false` / `true` | |

`hostpath` 条目字段:

| 字段 | 必填 | 默认 | 含义 |
|---|---|---|---|
| `type` | 是 | — | `"hostpath"` |
| `path` | 是 | — | 节点上的目录 |
| `mount_path` | 建议 | `/mnt/store-<i>` | 挂载点,需与 UCM `storage_backends` 一致 |
| `host_path_type` | 否 | 不设 | k8s hostPath type(如 `DirectoryOrCreate`) |
| `read_only` / `enable` | 否 | `false` / `true` | |

NFS 直挂写法示例(替换本示例里的 `storage`):

```json
"storage": [
  { "type": "nfs", "server": "192.168.10.100", "path": "/export/ucm", "mount_path": "/mnt/ucm" }
]
```

`dshm_size`(`motor_deploy_config` 顶层、独立):抬高 `/dev/shm`,容纳 CacheStore(默认 256GiB/DP,MLA 模型)。

**唯一硬约束**:UCM `ucm_connector_config.storage_backends` 必须等于对应 `storage[i].mount_path`。

## Mooncake master

Prefill 用 MultiConnector 会自动启用 `kvp-master`（mooncake_master），`kv_cache_store_config` 提供其端口与淘汰参数。多后端框架的默认 `backend` 是 `memcache`，此处必须显式声明 `"backend": "mooncake"`，否则拉起的是 memcache 服务，Mooncake transport 将找不到 master。
