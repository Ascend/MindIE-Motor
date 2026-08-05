# Mooncake 后端

Mooncake 池化后端，由 vllm-ascend 天然集成，**无需额外安装任何组件**。

## 配置

`AscendStoreConnector` 中配置 `"backend": "mooncake"`：

```json
"backend": "mooncake"
```

`kv_cache_store_config` 中配置 `"backend": "mooncake"`：

```json
"kv_cache_store_config": {
  "backend": "mooncake",
  "metadata_server": "P2PHANDSHAKE",
  "protocol": "ascend",
  "device_name": "",
  "global_segment_size": "1GB",
  "eviction_high_watermark_ratio": 0.9,
  "eviction_ratio": 0.1
}
```

`eviction_high_watermark_ratio` 和 `eviction_ratio` 为 Mooncake 专属参数，会传递给 `mooncake_master` 进程。

## 环境变量配置说明

按硬件在 `env.json` 的 `motor_engine_prefill_env`、`motor_engine_decode_env` 中配置下列环境变量，Prefill 与 Decode 保持一致。

| 硬件 | 依赖 | 环境变量（写入 `env.json`） | 说明 |
|------|------|------------------------------|------|
| Ascend 950 系列产品 | HDK >= 25.6 且 mooncake >= v0.3.11<br>CANN >= 9.1.0 | **UBOE**：`ASCEND_GLOBAL_RESOURCE_CONFIG={"comm_resource_config.protocol_desc":["uboe:device"]}`<br>**UB**：`ASCEND_LOCAL_COMM_RES={"version":"1.3"}` | 按实际使用的通信协议配置对应环境变量（UBOE / UB 二选一） |
| Atlas 800I/T A3 超节点服务器 | HDK >= 26.0<br>或 HDK >= 25.5 且 mooncake >= v0.3.11<br>CANN >= 9.0.0<br>灵衢算力网络 >= 1.5 | `ASCEND_ENABLE_USE_FABRIC_MEM=1` | **推荐**。启用统一内存地址直传方案。若开启 SSD offload，相关内存大小需按 1GB 对齐，详见 vllm-ascend 文档 [Fabric memory size alignment](https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/kv_pool.html#fabric-memory-size-alignment-a3-ascend-enable-use-fabric-mem-1) |
| Atlas 800I/T A3 超节点服务器 | 上述依赖不满足时 | `ASCEND_BUFFER_POOL=4:8` | 配置 NPU Device 上用于聚合与 KV 传输的 buffer 个数与大小（例如 `4:8` 表示 4 个 8MB buffer） |
| Atlas 800I/T A2 推理服务器 | 推荐 HDK >= 25.5 | `HCCL_INTRA_ROCE_ENABLE=1` | 800 I/T A2 系列直连传输方案所需 |

> 更多原理与排障请参考 [vllm-ascend KV Pool 文档](https://docs.vllm.ai/projects/ascend/zh-cn/main/user_guide/feature_guide/kv_pool.html)。

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
           echo "Warning: failed to detect default route interface from /proc/net/route, skip GLOO/TP/HCCL socket ifname env" >&2
           unset GLOO_SOCKET_IFNAME TP_SOCKET_IFNAME HCCL_SOCKET_IFNAME
       else
           export GLOO_SOCKET_IFNAME="$if_name"
           export TP_SOCKET_IFNAME="$if_name"
           export HCCL_SOCKET_IFNAME="$if_name"
       fi

       if [ -z "$ip" ]; then
           echo "Warning: HOST_IP and POD_IP are both empty, skip HCCL_IF_IP env" >&2
           unset HCCL_IF_IP
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
