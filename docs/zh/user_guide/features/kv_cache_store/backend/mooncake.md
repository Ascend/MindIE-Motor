# Mooncake 后端

Mooncake 池化后端，由 vllm-ascend 天然集成，**无需额外安装任何组件**。

## 配置（embedded 模式）

Mooncake 池化有两种部署方式（`store_mode` 取值），区别在池化内存由谁贡献：

- **embedded 模式（默认，`store_mode` 为空或 `"embedded"`）**：引擎进程自身贡献 `global_segment_size` 的池化内存，配置最简，适合小规模验证；引擎进程挂掉则其贡献的池化内存随之失效。本节介绍该模式，standalone 模式见[下节](#standalone-模式独立-store-进程)。
- **standalone 模式（`store_mode="standalone"`）**：由独立 `mooncake_store_service` 进程贡献池化内存，与引擎生命周期解耦，适合生产部署。

`kv_cache_store_config` 中配置 `"backend": "mooncake"`。可选配置 `target_job_id` 复用其他推理服务的 kv_store（值为目标服务的 `job_id`），行为说明见 [KV 池化 README — 多套服务共享 kv_store](../README.md#多套服务共享-kv_store)。

```json
"kv_cache_store_config": {
  "enable": true,
  "backend": "mooncake",
  "global_segment_size": "2GB",
  "eviction_high_watermark_ratio": 0.9,
  "eviction_ratio": 0.1
}
```

`eviction_high_watermark_ratio` 和 `eviction_ratio` 为 Mooncake 专属参数，会传递给 `mooncake_master` 进程；**deploy.py 对 mooncake 后端强制校验这两项，缺失会直接报错**，必须显式配置（建议值 0.9 / 0.1）。

该模式下需显式配置 `enable`（默认 `false`）、`backend`（默认 `memcache`）、`global_segment_size`（无默认）与上述两个驱逐参数，其余字段均有默认值。

## standalone 模式（独立 store 进程）

默认（embedded）模式下，`global_segment_size` 由每个引擎进程自己贡献，引擎进程挂掉则其贡献的池化内存随之失效。standalone 模式把内存贡献者从引擎进程中剥离：

- 每个 PD 实例 Pod 内由 NodeManager 拉起一个独立的 `mooncake_store_service` 进程（Mooncake 官方 store 入口），负责向池里贡献 `global_segment_size` 内存；
- 引擎进程 `global_segment_size=0`，仅作为请求方（配置由部署脚本自动生成，无需手工改）；
- `mooncake_master` 仍然运行在独立的 kv-store Pod 中。

收益：store 进程独立申请大内存（先于引擎启动，避免与引擎权重/KV 内存竞争），且引擎故障/重建不影响池内已有数据；store 进程故障由 NodeManager 原地重拉（受 `MOTOR_RESTART_LOCAL_SERVICE` 控制，默认开启），重新注册回 master。

### 配置

以下为 P/D 分离 + standalone 部署在 `user_config.json` 中的关键配置：

```json
"kv_cache_store_config": {
  "enable": true,
  "backend": "mooncake",
  "store_mode": "standalone",
  "global_segment_size": "200GB",
  "eviction_high_watermark_ratio": 0.9,
  "eviction_ratio": 0.1
}

"motor_engine_prefill_config": {
  "engine_type": "vllm",
  "engine_config": {
    // 模型、并行度等常规配置省略
    "kv_transfer_config": {
      "kv_connector": "MultiConnector",
      "kv_role": "kv_producer",
      "kv_port": "30001",
      "engine_id": "0",
      "kv_connector_extra_config": {
        "connectors": [
          {
            "kv_connector": "MooncakeConnectorV1",
            "kv_role": "kv_producer",
            "kv_port": "30001",
            "kv_connector_extra_config": {
              "prefill": {"dp_size": 1, "tp_size": 2, "pp_size": 1},
              "decode": {"dp_size": 1, "tp_size": 2, "pp_size": 1}
            }
          },
          {
            "kv_connector": "AscendStoreConnector",
            "kv_role": "kv_producer",
            "kv_connector_extra_config": {
              "backend": "mooncake"
            }
          }
        ]
      }
    }
  }
}

"motor_engine_decode_config": {
  "engine_type": "vllm",
  "engine_config": {
    // 结构同 prefill，仅 kv_role 相反：
    //   "kv_role": "kv_consumer"
    //   MooncakeConnectorV1 与 AscendStoreConnector 的 kv_role 均为 "kv_consumer"
  }
}
```

### 字段说明

`kv_cache_store_config`：

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `enable` | `false` | 池化总开关，需配 `true` |
| `backend` | `memcache` | 需配 `"mooncake"` |
| `store_mode` | `embedded` | standalone 模式需配 `"standalone"` |
| `global_segment_size` | 无 | standalone 下为 store 进程贡献的池化内存（如 `"200GB"`） |
| `eviction_high_watermark_ratio` / `eviction_ratio` | 无（**必填**） | 驱逐水位与单次驱逐比例，传递给 `mooncake_master`；deploy.py 强制校验，缺失报错（建议 0.9 / 0.1） |

`engine_config.kv_transfer_config`：

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `kv_connector` | — | 需配 `"MultiConnector"` |
| `kv_role` | — | 需配：prefill = `kv_producer`，decode = `kv_consumer` |
| `engine_id` | — | 池化域标识，需配且 P/D 保持一致 |
| `connectors[].MooncakeConnectorV1` | — | P2P 直传（prefill → decode 直接传 KV，不落池）；`prefill`/`decode` 拓扑需与部署一致 |
| `connectors[].AscendStoreConnector` | — | 存池（prefill 入池 / decode 出池）；`backend` 需配 `"mooncake"` |
| `kv_port` | — | 建议显式指定（如 `"30001"`），避免多服务端口冲突 |

> store 进程的配置文件由 NodeManager 自动生成在引擎配置同目录（`mooncake_store_config.json`），`local_hostname` 取 `POD_IP`，master 地址取 `KVS_MASTER_SERVICE`。
>
> store 进程的通信环境同样由 NodeManager 自动配置，**无需手工干预**：store 独占 HIXL `comm_resource_config.listen_port=26666`（写入 ranktable 的 `device_port`，与同卡引擎 worker 区分，避免 HCCL `EI0014`），HCCL socket 端口段相对引擎偏移（A2：`HCCL_NPU_SOCKET_PORT_RANGE=16700-16800`；A5：host socket 段 +2000），避免与引擎的 16666/RA socket 冲突（`EI0020`）。

## 环境变量配置说明

在 `env.json` 的 `motor_engine_prefill_env`、`motor_engine_decode_env` 中配置下列环境变量，Prefill 与 Decode 保持一致。

**所有硬件均需配置：**

| 环境变量（写入 `env.json`） | 说明 |
|------|------|
| `HCCL_INTRA_ROCE_ENABLE=1` | **必须**。HIXL 底层直连传输走 RoCE 协议，需显式使能才能建连，未配置会导致 KV 传输失败。注意该变量需配在 `motor_engine_prefill_env` / `motor_engine_decode_env` 中并随部署下发到引擎 Pod，仅在部署节点 shell 中 export 不生效 |
| `ASCEND_LOCAL_COMM_RES={"version":"1.3"}` | **必须**。使 ascend_transport 按 v1.3 格式生成本地通信资源，走 client-server 单边通信，ranktable 携带 `device_port`。所有硬件（A2/A5 等）均需配置；standalone 模式下缺失时，store 进程与引擎 worker 共用同一 NPU 会因合并 ranktable 出现重复 device_ip 报 `EI0014: IP is used repeatedly`，与 store 同卡的 worker（如 TP rank 0）block 入池失败 |

`env.json` 配置示例（所有硬件通用，Prefill 与 Decode 相同）：

```json
"motor_engine_prefill_env": {
  "HCCL_INTRA_ROCE_ENABLE": "1",
  "ASCEND_LOCAL_COMM_RES": "{\"version\":\"1.3\"}"
}
```

**各硬件依赖与差异化配置（CANN 版本均需 >= 9.1.0）：**

| 硬件 | 依赖 | 环境变量（写入 `env.json`） | 说明 |
|------|------|------------------------------|------|
| Ascend 950 系列产品 | HDK >= 25.6 且 mooncake >= v0.3.11<br>CANN >= 9.1.0 | **UBOE**：`ASCEND_GLOBAL_RESOURCE_CONFIG={"comm_resource_config.protocol_desc":["uboe:device"]}`<br>**UB**：`ASCEND_LOCAL_COMM_RES={"version":"1.3"}` | 按实际使用的通信协议配置对应环境变量（UBOE / UB 二选一） |
| Atlas 800I/T A3 超节点服务器 | HDK >= 26.0<br>或 HDK >= 25.5 且 mooncake >= v0.3.11<br>CANN >= 9.1.0<br>灵衢算力网络 >= 1.5 | `ASCEND_ENABLE_USE_FABRIC_MEM=1` | **推荐**。启用统一内存地址直传方案。若开启 SSD offload，相关内存大小需按 1GB 对齐，详见 vllm-ascend 文档 [Fabric memory size alignment](https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/kv_pool.html#fabric-memory-size-alignment-a3-ascend-enable-use-fabric-mem-1) |
| Atlas 800I/T A3 超节点服务器 | 上述依赖不满足时 | `ASCEND_BUFFER_POOL=4:8` | 配置 NPU Device 上用于聚合与 KV 传输的 buffer 个数与大小（例如 `4:8` 表示 4 个 8MB buffer） |
| Atlas 800I/T A2 推理服务器 | HDK >= 25.5<br>CANN >= 9.1.0 | — | 无需额外环境变量，通用必配项即可 |

> 更多原理与排障请参考 [vllm-ascend KV Pool 文档](https://docs.vllm.ai/projects/ascend/zh-cn/main/user_guide/feature_guide/kv_pool.html)。

## 入池条件

block 入池的前提是**请求的 prompt 长度不小于 128 token**：`AscendStoreConnector` 按 128 token 的 chunk 粒度做入池判定（`can_save`），只有 token 数达到一个完整 chunk 时才真正触发存池 put。短请求（prompt < 128 token）不会入池，也不会产生 put 流量，这是 vllm-ascend 的设计行为，不是故障。

验证池化是否生效时请使用长 prompt（≥ 128 token，建议 500+ token）发起请求，并通过 master 侧观测 `PutStart`/`Keys` 等指标确认 block 确实入池。

## 调优建议

- **`global_segment_size`**：根据模型大小和并发量调整，过小会导致频繁驱逐；过大则浪费显存。建议设为模型 KV Cache 预估大小的 1.5~2 倍。
- **`eviction_high_watermark_ratio`** 与 **`eviction_ratio`**：当池化空间使用率达到 `eviction_high_watermark_ratio` 时触发驱逐，每次驱逐 `eviction_ratio` 比例的空间。高并发场景可适度降低驱逐比例以减少抖动。
- **`default_kv_lease_ttl`**：控制 KV 对象的租约有效期，需确保大于传输超时时间（`ASCEND_CONNECT_TIMEOUT` / `ASCEND_TRANSFER_TIMEOUT`），避免租约在传输完成前过期。

## Ascend 950 系列服务器额外配置说明

在 Atlas 850 超节点服务器 上使用 Mooncake 后端做 KV 池化时，当前需要保证 Pod 能访问宿主机侧 UB 相关网卡（`ipourma*`）。任选以下方式之一。

### 方式一：使用 host 网络

1. **配置 Prefill / Decode 的 host 网络**

   默认 CRD 部署（`deploy_mode` 为 `infer_service_set`）时，编辑 `examples/deployer/yaml_template/infer_service_template.yaml`，在 `roles` 中 `- name: prefill` 与 `- name: decode` 两段定义的 `spec.template.spec` 下分别增加如下字段（两处均需配置）：

   ```yaml
   - name: prefill   # decode 角色同样修改
     # ...
     spec:
       template:
         spec:
           hostNetwork: true
           dnsPolicy: ClusterFirstWithHostNet
           schedulerName: volcano
           # ... 其余原有字段保持不变
   ```

   > 若 `user_config.json` 中 `motor_deploy_config.deploy_mode` 为 `multi_deployment`，则修改 `examples/deployer/yaml_template/engine_template.yaml`：在引擎 Pod 的 `spec.template.spec` 下增加与上述相同的字段（`hostNetwork: true`、`dnsPolicy: ClusterFirstWithHostNet`）。

2. **补充 Atlas 850 超节点服务器 引擎环境变量**

   编辑 `examples/deployer/startup/common.sh` 中的 `set_a5_engine_env`（由 `roles/engine.sh` 在 Atlas 850 超节点服务器 场景调用），补齐如下实现：

   ```bash
   set_a5_engine_env() {
       local if_name ip
       if_name=$(awk '$2 == "00000000" {print $1; exit}' /proc/net/route)
       ip="${HOST_IP:-$POD_IP}"

       if [ -z "$if_name" ]; then
           # Skip auto-detection only; never unset — the user may have exported these explicitly.
           echo "Warning: failed to detect default route interface from /proc/net/route, skip GLOO/TP/HCCL socket ifname env" >&2
       else
           export GLOO_SOCKET_IFNAME="$if_name"
           export TP_SOCKET_IFNAME="$if_name"
           export HCCL_SOCKET_IFNAME="$if_name"
       fi

       if [ -z "$ip" ]; then
           # Skip auto-detection only; never unset — the user may have exported it explicitly.
           echo "Warning: HOST_IP and POD_IP are both empty, skip HCCL_IF_IP env" >&2
       else
           export HCCL_IF_IP="$ip"
       fi

       export PATH="$PATH:/usr/local/go/bin"
       export LD_LIBRARY_PATH="/usr/local/lib:/usr/lib64:/lib64:${LD_LIBRARY_PATH:-}"
       export ASCEND_LOCAL_COMM_RES_PATH="${ASCEND_LOCAL_COMM_RES_PATH:-/etc/hixlep}"
   }
   ```

   > 上述脚本从 `/proc/net/route` 选取默认路由的第一张网卡作为 `GLOO`/`TP`/`HCCL` 通信网卡，请确保该网卡为服务器的主网卡。

   完成上述修改后重新部署服务。

### 方式二：将宿主机 ipourma 网卡挂入 Pod

需在**每一台部署了推理实例的服务器**上分别执行下述挂载操作。

**挂入 Pod：**

1. **备份 IPv6（必须）**

   ```bash
   BACKUP=/tmp/ipourma_backup_latest.txt
   : > "$BACKUP"
   for i in $(seq 0 9); do
     echo "===== ipourma$i =====" | tee -a "$BACKUP"
     ip -6 addr show dev ipourma$i 2>/dev/null | tee -a "$BACKUP"
   done
   ```

2. **找到业务容器并查 PID**

   在本机执行 `docker ps`，按容器名区分 pause 沙箱与推理业务容器。Docker 下 K8s 容器名大致为：

   ```text
   k8s_<容器名>_<Pod名>_<命名空间>_<PodUID>_<重启次数>
   ```

   | 片段 | 含义 | 示例 |
   |------|------|------|
   | `k8s_POD_...` | pause 沙箱，**不要用** | `k8s_POD_vllm-0-decode-0-0_mindie-motor_...` |
   | `k8s_vllm_...` | **业务容器**（镜像里容器名常为 `vllm`） | `k8s_vllm_vllm-0-decode-0-0_mindie-motor_...` |

   上表第二行 `k8s_vllm_...` 即为业务容器，后续取 PID、挂网卡均针对它操作。

   ```bash
   # 将 <Pod名关键词> 换成实际值，如 vllm-0-decode-0-0；排除 k8s_POD_ 沙箱
   CNAME=$(docker ps --format '{{.Names}}' | grep '<Pod名关键词>' | grep -v 'k8s_POD_' | head -1)
   PID=$(docker inspect -f '{{.State.Pid}}' "$CNAME")
   echo "CNAME=$CNAME PID=$PID"
   ```

3. **暴露网络命名空间**

   将 `<自定义名>` 替换为便于记忆的名称（如 `decode`、`prefill`）：

   ```bash
   NS_NAME=<自定义名>
   mkdir -p /var/run/netns
   ln -sf /proc/$PID/ns/net /var/run/netns/$NS_NAME
   ip netns list
   ```

4. **把网卡移进 Pod**

   ```bash
   for i in $(seq 0 9); do
     ip link show dev ipourma$i >/dev/null 2>&1 && ip link set ipourma$i netns "$NS_NAME"
   done
   ```

5. **在 Pod netns 内开启 IPv6、拉起网卡并恢复地址**

   ```bash
   ip netns exec "$NS_NAME" sysctl -w net.ipv6.conf.all.disable_ipv6=0
   ip netns exec "$NS_NAME" sysctl -w net.ipv6.conf.default.disable_ipv6=0

   cur_dev=""
   while IFS= read -r line; do
     if [[ "$line" =~ ^=====[[:space:]]+(ipourma[0-9]+) ]]; then
       cur_dev="${BASH_REMATCH[1]}"
       ip netns exec "$NS_NAME" sysctl -w net.ipv6.conf.${cur_dev}.disable_ipv6=0 || true
       ip netns exec "$NS_NAME" ip link set "$cur_dev" up || true
     elif [[ "$line" =~ inet6[[:space:]]+([^/]+)/([0-9]+) ]]; then
       addr="${BASH_REMATCH[1]}"; pref="${BASH_REMATCH[2]}"
       [ -n "$cur_dev" ] && [ "$addr" != "::1" ] && \
         ip netns exec "$NS_NAME" ip -6 addr add "$addr/$pref" dev "$cur_dev" 2>/dev/null || true
     fi
   done < "$BACKUP"
   ```

6. **验证**

   ```bash
   ip netns exec "$NS_NAME" ip -br link | grep ipourma
   nsenter -t "$PID" -n python3 -c "import socket; print(socket.if_nametoindex('ipourma0'))"
   ```

   挂载成功后，容器内应能看到 `ipourma0`～`ipourma9` 等网卡（具体名称与地址以实际环境为准）。

**移回宿主机（回退）：**

删除或重建 Pod **之前**执行。`<当时使用的名字>` 须与挂入时的 `NS_NAME` 一致：

```bash
NS_NAME=<当时使用的名字>
BACKUP=/tmp/ipourma_backup_latest.txt

# 1. 从 Pod netns 挪回宿主机
for i in $(seq 0 9); do
  ip netns exec "$NS_NAME" ip link show dev ipourma$i >/dev/null 2>&1 && \
    ip netns exec "$NS_NAME" ip link set ipourma$i netns 1
done

# 2. 宿主机恢复 IPv6
sysctl -w net.ipv6.conf.all.disable_ipv6=0
sysctl -w net.ipv6.conf.default.disable_ipv6=0

cur_dev=""
while IFS= read -r line; do
  if [[ "$line" =~ ^=====[[:space:]]+(ipourma[0-9]+) ]]; then
    cur_dev="${BASH_REMATCH[1]}"
    sysctl -w net.ipv6.conf.${cur_dev}.disable_ipv6=0 || true
    ip link set "$cur_dev" up || true
  elif [[ "$line" =~ inet6[[:space:]]+([^/]+)/([0-9]+) ]]; then
    addr="${BASH_REMATCH[1]}"; pref="${BASH_REMATCH[2]}"
    [ -n "$cur_dev" ] && [ "$addr" != "::1" ] && \
      ip -6 addr add "$addr/$pref" dev "$cur_dev" 2>/dev/null || true
  fi
done < "$BACKUP"

# 3. 删除软链
rm -f /var/run/netns/"$NS_NAME"

# 4. 确认
ip -br link | grep ipourma
```

>[!NOTE]说明
>
>- 一块 `ipourma` 同一时刻只能属于一个网络命名空间；同节点多个引擎 Pod 不要抢同一批口。
>- 未回退就删除 Pod，网卡可能丢失，需重载驱动或重启节点才能恢复。
>- 若报 `IPv6 is disabled on this device`，先对该口执行 `sysctl -w net.ipv6.conf.<网卡>.disable_ipv6=0`。
