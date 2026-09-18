# Mooncake 后端特性

## 特性介绍

**依据原文上下文内容重组，请进行人工校验。**

Mooncake 后端通过分布式共享内存池化机制实现跨引擎 KV 缓存共享，由 vLLM Ascend 天然集成，**无需额外安装任何组件**。该特性允许 Prefill 引擎将 KV Cache 存入共享池，Decode 引擎从池中读取，实现 P/D 分离场景下的 KV 缓存复用，减少重复计算，显著降低显存占用。

### 工作原理

**依据原文上下文内容重组，请进行人工校验。**

Mooncake 后端采用分布式共享内存池架构，通过 `mooncake_master` 进程管理全局池化元数据，各引擎节点通过 `mooncake_store` 进程贡献或消费池化内存。其核心工作流程如下：

1. **池化内存注册**：各节点通过 mooncake_master 注册自身存储节点信息，形成全局可见的分布式 KV 池。
2. **KV 入池**：Prefill 引擎通过 AscendStoreConnector 将计算完成的 KV Cache 按 128 token 分块写入共享池。
3. **KV 出池**：Decode 引擎通过 AscendStoreConnector 从共享池读取所需的 KV Cache 块。
4. **P2P 直传**：支持通过 MooncakeConnectorV1 实现 Prefill 到 Decode 的直接传输（不落池），降低延迟。

Mooncake 池化有两种部署方式（store_mode 取值），区别在于池化内存由谁贡献：

- **embedded 模式**（默认，store_mode 为空或 "embedded"）：引擎进程自身贡献 `global_segment_size` 的池化内存，配置最简单，适合小规模验证。**注意**：引擎进程挂掉则其贡献的池化内存随之失效。
- **standalone 模式**（store_mode="standalone"）：由独立 mooncake_store_service 进程贡献池化内存，与引擎生命周期解耦，适合生产部署。store 进程独立申请大内存（先于引擎启动，避免与引擎权重/KV 内存竞争），且引擎故障/重建不影响池内已有数据；store 进程故障由 NodeManager 原地重拉（受 MOTOR_RESTART_LOCAL_SERVICE 控制，默认开启），重新注册回 master。

### 核心功能

- **跨引擎 KV 共享**：Prefill 与 Decode 引擎之间共享 KV Cache，避免重复计算。
- **P2P 直传**：支持 Prefill 到 Decode 的 KV 直接传输（MooncakeConnectorV1），不经过存储池，降低传输延迟。
- **自动驱逐**：支持基于水位的自动驱逐机制（eviction_high_watermark_ratio / eviction_ratio），当池化空间使用率达到阈值时自动驱逐旧数据。
- **多服务共享**：支持通过 target_job_id 配置跨服务共享 KV 存储池。
- **多硬件适配**：支持 Atlas 850 超节点服务器、Atlas 800I A3 超节点服务器、Atlas 800I A2 推理服务器等多种硬件平台。

### 约束与限制

**表 1** <a id="teble001"></a>硬件约束

| 硬件 | 依赖 | 说明 |
|------|------|------|
| Atlas 850 超节点服务器 | <ul><li>HDK >= 25.6</li><li>mooncake >= v0.3.11</li><li>CANN >= 9.1.0</li></ul>  | 需根据实际通信协议配置 UBOE 或 UB 环境变量。 |
| Atlas 800I A3 超节点服务器 | <ul><li>HDK >= 26.0 或（HDK >= 25.5 且 mooncake >= v0.3.11）</li><li>CANN >= 9.1.0</li><li>灵衢算力网络 >= 1.5</li></ul> | 推荐启用统一内存地址直传方案。 |
| Atlas 800I A2 推理服务器 | <ul><li>HDK >= 25.5</li><li>CANN >= 9.1.0</li></ul> | 无需额外环境变量，通用必配项即可。 |

>[!NOTE] 说明
>
>- 所有硬件均需配置 `HCCL_INTRA_ROCE_ENABLE=1` 和 `ASCEND_LOCAL_COMM_RES={"version":"1.3"}`环境变量，缺失会导致 KV 传输失败或报 `EI0014` 错误。
>- `deploy.py` 对 Mooncake 后端强制校验 `eviction_high_watermark_ratio` 和 `eviction_ratio` 两项参数，缺失会直接报错，必须显式配置。
>- embedded 模式下，引擎进程挂掉则其贡献的池化内存随之失效。
>- standalone 模式下，store 进程的配置文件由 NodeManager 自动生成，无需手工配置。
>- 短请求（prompt < 128 token）不会入池，也不会产生 put 流量，这是 vLLM Ascend 的设计行为，而非故障。

## 特性使用

### 环境准备

**通用环境变量配置（所有硬件均需配置）：**

在 env.json 的 motor_engine_prefill_env、motor_engine_decode_env 中配置下列环境变量，Prefill 与 Decode 保持一致。

```json
"motor_engine_prefill_env": {
  "HCCL_INTRA_ROCE_ENABLE": "1",
  "ASCEND_LOCAL_COMM_RES": "{\"version\":\"1.3\"}"
}
```

**表 2** 环境变量说明

| 环境变量 | 说明 |
|------|------|
| HCCL_INTRA_ROCE_ENABLE=1 | **必须**。<ul><li>HIXL 底层直连传输走 RoCE 协议，需显式使能才能建连，未配置会导致 KV 传输失败。</li><li>该变量需配在 motor_engine_prefill_env / motor_engine_decode_env 中并随部署下发到引擎 Pod，仅在部署节点 shell 中 export 不生效。</li></ul> |
| ASCEND_LOCAL_COMM_RES={"version":"1.3"} | **必须**。<ul><li>使 ascend_transport 按 v1.3 格式生成本地通信资源，走 client-server 单边通信，ranktable 携带 `device_port`，所有硬件均需配置；</li><li>standalone 模式下缺失时，store 进程与引擎 worker 共用同一 NPU 会因合并 ranktable 出现重复 device_ip 报 `EI0014: IP is used repeatedly`，与 store 同卡的 worker（如 TP rank 0）block 入池失败。</li></ul> |

**表 3** 各硬件差异化环境变量配置

| 硬件 | 环境变量 | 说明 |
|------|-------|------|
| Atlas 850 超节点服务器 | <ul><li>使用 UBOE 协议时：ASCEND_GLOBAL_RESOURCE_CONFIG={"comm_resource_config.protocol_desc":["uboe:device"]}</li><li>使用 UB 协议时：ASCEND_LOCAL_COMM_RES={"version":"1.3"}</li></ul> | UBOE / UB 二选一，按实际使用的通信协议配置。 |
| Atlas 800I A3 超节点服务器 | ASCEND_ENABLE_USE_FABRIC_MEM=1 | **推荐方案**。<br>启用统一内存地址直传方案。若开启 SSD offload，相关内存大小需按 1GB 对齐,详见 vLLM Ascend 文档 [Fabric memory size alignment](https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/kv_pool.html#fabric-memory-size-alignment-a3-ascend-enable-use-fabric-mem-1)。 |
| Atlas 800I A3 超节点服务器 | ASCEND_BUFFER_POOL=4:8 | **推荐方案**，当[表 1](#teble001)依赖列软件版本不满足时，使用该方案。<br>配置 NPU Device 上用于聚合与 KV 传输的 buffer 个数与大小（例如 `4:8` 表示 4 个 8MB buffer）。 |
| Atlas 800I A2 推理服务器 | — | 无需额外环境变量，通用必配项即可。 |

### 使用场景

**场景一：embedded 模式（小规模验证）**

- 适用场景：开发测试环境、小规模验证、快速原型。
- 特点：配置最简单，引擎进程自身贡献池化内存，无需额外部署 store 进程。
- 限制：引擎进程挂掉则池化内存随之失效，不适合生产环境。

**场景二：standalone 模式（生产部署）**

- 适用场景：生产环境、P/D 分离部署、高可用要求。
- 特点：由独立 `mooncake_store_service` 进程贡献池化内存，与引擎生命周期解耦，引擎故障/重建不影响池内已有数据。
- 收益：store 进程独立申请大内存，避免与引擎权重/KV 内存竞争；store 进程故障可由 NodeManager 自动恢复。

### 使用样例

**依据原文上下文内容重组，请进行人工校验。**

#### 场景一：embedded 模式配置

1. 在 user_config.json配置文件 中配置 Mooncake 后端字段 kv_cache_store_config，样例如下。

    ```json
    "kv_cache_store_config": {
      "enable": true,
      "backend": "mooncake",
      "global_segment_size": "2GB",
      "eviction_high_watermark_ratio": 0.9,
      "eviction_ratio": 0.1
    }
    ```

    **表 4** 参数说明

    | 字段 | 默认值 | 必填 | 说明 |
    |------|--------|------|------|
    | enable | false | 是 | 池化总开关，需配置为：true。 |
    | backend | memcache | 是 | 需配置为： "mooncake"。 |
    | global_segment_size | 无 | 是 | 引擎进程贡献的池化内存大小，如： "2GB"。 |
    |`eviction_high_watermark_ratio | 无 | 是 | 驱逐水位阈值，建议值为： 0.9；`deploy.py` 强制校验，缺失报错。 |
    | eviction_ratio | 无 | 是 | 单次驱逐比例，建议值为：0.1；`deploy.py` 强制校验，缺失报错。 |

2. 参考[环境准备](#环境准备)章节在 `env.json` 中配置通用环境变量，确保 Prefill 和 Decode 配置一致。

3. 使用部署脚本启动服务。（**缺少命令，需要补充**）

    部署后日志应出现 `mooncake master` 连接成功的标志，则表示 kv_cache_store 初始化完成。

#### 场景二：standalone 模式配置

下方示例 `connectors[0]` 为标准 attention 配置，混合 attention 须换为 `MooncakeHybridConnector`，详情请参见 [选型说明](../../../features/kv_cache_store/README.md)。

1. 在 user_config.json 配置文件中配置 kv_cache_store_config 字段。

    ```json
    "kv_cache_store_config": {
      "enable": true,
      "backend": "mooncake",
      "store_mode": "standalone",
      "global_segment_size": "200GB",
      "eviction_high_watermark_ratio": 0.9,
      "eviction_ratio": 0.1
    }
    ```

    **表 5** 参数说明

    | 字段 | 默认值 | 必填 | 说明 |
    |------|--------|------|------|
    | enable | false | 是 | 池化总开关，需配置为： true 。|
    | backend | memcache | 是 | 需配置为： "mooncake" 。|
    | store_mode | embedded | 是 | standalone 模式需配置为： "standalone"。 |
    | global_segment_size | 无 | 是 | store 进程贡献的池化内存大小，如 ："200GB"。 |
    | eviction_high_watermark_ratio | 无 | 是 | 驱逐水位阈值，建议 0.9；`deploy.py` 强制校验，缺失报错。 |
    | eviction_ratio | 无 | 是 | 单次驱逐比例，建议 0.1；`deploy.py` 强制校验，缺失报错。 |

2. 在 user_config.json 配置文件中配置 `engine_config.kv_transfer_config`子字段。
   - motor_engine_prefill_config（Prefill）

      ```json
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
      ```

   - motor_engine_decode_config（Decode）

      ```json
      "motor_engine_decode_config": {
        "engine_type": "vllm",
        "engine_config": {
          // 模型、并行度等常规配置省略
          "kv_transfer_config": {
            "kv_connector": "MultiConnector",
            "kv_role": "kv_consumer",
            "kv_port": "30001",
            "engine_id": "0",
            "kv_connector_extra_config": {
              "connectors": [
                {
                  "kv_connector": "MooncakeConnectorV1",
                  "kv_role": "kv_consumer",
                  "kv_port": "30001",
                  "kv_connector_extra_config": {
                    "prefill": {"dp_size": 1, "tp_size": 2, "pp_size": 1},
                    "decode": {"dp_size": 1, "tp_size": 2, "pp_size": 1}
                  }
                },
                {
                  "kv_connector": "AscendStoreConnector",
                  "kv_role": "kv_consumer",
                  "kv_connector_extra_config": {
                    "backend": "mooncake"
                  }
                }
              ]
            }
          }
        }
      }
      ```

    **表 6** kv_transfer_config 参数说明

    | 字段 | 默认值 | 必填 | 说明 |
    |------|--------|------|------|
    | kv_connector | — | 是 | 需配置为： "MultiConnector" |
    | kv_role | — | 是 | <ul><li>Prefill：配置为 "kv_producer"</li><li>Decode 配置为 "kv_consumer"</li></ul> |
    | engine_id | — | 是 | 池化域标识，需配置且 P/D 保持一致 |
    | kv_port | — | 建议 | 建议显式指定（如 `"30001"`），避免多服务端口冲突 |
    | connectors[].MooncakeConnectorV1 | — | 是 | P2P 直传（prefill → decode 直接传 KV，不落池）；`prefill`/`decode` 拓扑需与部署一致 |
    | connectors[].AscendStoreConnector | — | 是 | 存池（prefill 入池 / decode 出池）；`backend` 需配 `"mooncake"` |

3. 参考[环境准备](#环境准备)章节在 `env.json` 中配置通用环境变量，确保 Prefill 和 Decode 配置一致。

4. 使用部署脚本启动服务。（**缺少命令，需要补充**）
  NodeManager 会自动拉起 mooncake_store_service 进程，自动生成 store 配置文件。
  部署完成后：
   - 日志应出现 mooncake master 连接成功标志。
   - store 进程配置文件自动生成在引擎配置同目录（mooncake_store_config.json）。
   - Prefill 和 Decode 引擎均成功注册到 mooncake_master。

    >[!NOTE] 说明
    >
    >- store 进程的配置文件由 NodeManager 自动生成在引擎配置同目录（mooncake_store_config.json），local_hostname 取 POD_IP，master 地址取 KVS_MASTER_SERVICE。
    >- store 进程的通信环境同样由 NodeManager 自动配置，无需手工干预：store 独占 HIXL comm_resource_config.listen_port=26666（写入 ranktable 的 device_port，与同卡引擎 worker 区分，避免 HCCL EI0014），HCCL socket 端口段相对引擎偏移（A2：HCCL_NPU_SOCKET_PORT_RANGE=16700-16800；A5：host socket 段 +2000），避免与引擎的 16666/RA socket 冲突（EI0020）。

#### 场景三：Atlas 850 超节点服务器额外配置

在 Atlas 850 超节点服务器上使用 Mooncake 后端时，需确保 Pod 能访问宿主机侧 UB 相关网卡（ipourma*），任选以下方式之一。

**方式一：使用 host 网络**

1. 配置 Prefill / Decode 的 host 网络。
   默认 CRD 部署（`deploy_mode` 为 `infer_service_set`）时，编辑 examples/deployer/yaml_template/infer_service_template.yaml，在 roles 中 - name: prefill 与 - name: decode 两段定义的 spec.template.spec 下分别增加如下字段（两处均需配置）：

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

   >[!NOTE] 说明
   >若 user_config.json 中 motor_deploy_config.deploy_mode 为multi_deployment，则修改 examples/deployer/yaml_template/engine_template.yaml：在引擎 Pod 的 spec.template.spec 下增加与上述相同的字段（hostNetwork: true、dnsPolicy: ClusterFirstWithHostNet）。

2. 补充 Atlas 850 超节点服务器 引擎环境变量。
   编辑 examples/deployer/startup/common.sh 中的 set_a5_engine_env（由 roles/engine.sh 在 Atlas 850 超节点服务器 场景调用），补齐示例如下：

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

   >[!NOTE] 说明
   >上述脚本从 /proc/net/route 选取默认路由的第一张网卡作为 `GLOO`/`TP`/`HCCL` 通信网卡，请确保该网卡为服务器的主网卡。

3. 重新部署服务。（**缺少命令，需要补充**）

**方式二：将宿主机 ipourma 网卡挂入 Pod**

需在**每一台部署了推理实例的服务器**上分别执行下述挂载操作。

**挂入 Pod：**

1. 备份 IPv6。

   ```bash
   BACKUP=/tmp/ipourma_backup_latest.txt
   : > "$BACKUP"
   for i in $(seq 0 9); do
     echo "===== ipourma$i =====" | tee -a "$BACKUP"
     ip -6 addr show dev ipourma$i 2>/dev/null | tee -a "$BACKUP"
   done
   ```

2. 找到业务容器并查 PID。

   在本机执行 `docker ps`，按容器名区分 pause 沙箱与推理业务容器。Docker 下 K8s 容器名大致为：

   ```text
   k8s_<容器名>_<Pod名>_<命名空间>_<PodUID>_<重启次数>
   ```

   | 片段 | 含义 | 示例 |
   |------|------|------|
   | k8s_POD_... | pause 沙箱，无须使用 | k8s_POD_vllm-0-decode-0-0_mindie-motor_... |
   | k8s_vllm_... | 业务容器（镜像里容器名常为： vllm） |k8s_vllm_vllm-0-decode-0-0_mindie-motor_... |

   >[!NOTE] 说明
   >`k8s_vllm_...` 即为业务容器，后续取 PID、挂网卡均针对它操作。
   >
   >```bash
   ># 将 <Pod名关键词> 换成实际值，如 vllm-0-decode-0-0；排除 k8s_POD_ 沙箱
   >CNAME=$(docker ps --format '{{.Names}}' | grep '<Pod名关键词>' | grep -v 'k8s_POD_' | head -1)
   >PID=$(docker inspect -f '{{.State.Pid}}' "$CNAME")
   >echo "CNAME=$CNAME PID=$PID"
   >```

3. 暴露网络命名空间。

   将 <自定义名> 替换为便于记忆的名称（如 `decode`、`prefill`）：

   ```bash
   NS_NAME=<自定义名>
   mkdir -p /var/run/netns
   ln -sf /proc/$PID/ns/net /var/run/netns/$NS_NAME
   ip netns list
   ```

4. 把网卡移进 Pod。

   ```bash
   for i in $(seq 0 9); do
     ip link show dev ipourma$i >/dev/null 2>&1 && ip link set ipourma$i netns "$NS_NAME"
   done
   ```

5. 在 Pod netns 内开启 IPv6、拉起网卡并恢复地址。

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

6. 使用以下命令进行验证。

   ```bash
   ip netns exec "$NS_NAME" ip -br link | grep ipourma
   nsenter -t "$PID" -n python3 -c "import socket; print(socket.if_nametoindex('ipourma0'))"
   ```

   挂载成功后，容器内应能看到 `ipourma0`～`ipourma9` 等网卡（具体名称与地址以实际环境为准）。

**移回宿主机（回退）：**

删除或重建 Pod 之前执行。<当时使用的名字> 须与挂入时的 NS_NAME 一致：

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

>[!NOTE] 说明
>
>- 一块 ipourma 同一时刻只能属于一个网络命名空间；同节点多个引擎 Pod 不要抢同一批口。
>- 未回退就删除 Pod，网卡可能丢失，需重载驱动或重启节点才能恢复。
>- 若报 IPv6 is disabled on this device，先对该口执行 sysctl -w net.ipv6.conf.<网卡>.disable_ipv6=0。

### 验证特性

**依据原文上下文内容重组，请进行人工校验。**

**验证前提：** 使用长 prompt（≥ 128 token，建议 500+ token）发起请求。短请求（prompt < 128 token）不会入池，也不会产生 put 流量，而非故障。

**验证步骤：**

1. 使用 curl 或类似工具向 `mooncake_master` 服务查询指标：

   ```bash
   curl http://<master-ip>:<master-port>/metrics | grep -E "PutStart|Keys"
   ```

2. 检查 `mooncake_master` 的日志，确认存在 KV block 入池的记录。

3. 观察 Decode 引擎的日志，确认存在从池中读取 KV block 的记录。

**预期输出：**

- `PutStart` 计数应大于 0，表示有 block 成功入池。
- `Keys` 列表中应存在对应 block 的 key 标识。
- 若使用 `MooncakeConnectorV1` P2P 直传，可观察到 Prefill 到 Decode 的直接传输记录。

## 调优建议

**依据原文上下文内容重组，请进行人工校验。**

| 参数 | 场景 | 推荐配置 | 说明 |
|------|------|----------|------|
| global_segment_size | 通用 | 模型 KV Cache 预估大小的 1.5~2 倍 | 根据模型大小和并发量调整，过小会导致频繁驱逐，过大则浪费显存。 |
| eviction_high_watermark_ratio | 通用 | 0.9 | 高并发场景可适度降低以减少驱逐抖动。 |
| eviction_ratio | 通用 | 0.1 | 高并发场景可适度降低以减少驱逐抖动。 |
| default_kv_lease_ttl | 高延迟场景 | 大于传输超时时间（ASCEND_CONNECT_TIMEOUT / ASCEND_TRANSFER_TIMEOUT） | 控制 KV 对象的租约有效期，避免租约在传输完成前过期。 |

**参数联动说明：**

- `global_segment_size` 与 `eviction_high_watermark_ratio` 共同决定触发驱逐的实际内存阈值：`实际触发阈值 = global_segment_size × eviction_high_watermark_ratio`。
- 高并发场景下，建议同时降低 `eviction_high_watermark_ratio` 和 `eviction_ratio`，以减少驱逐操作对业务抖动的叠加影响。
- `default_kv_lease_ttl` 需与传输超时配合设置，确保租约存活时间大于最慢的传输完成时间，避免 KV 传输中途被回收。

## 常见问题

**依据原文上下文内容重组，请进行人工校验。**

### 部署后报 EI0014: IP is used repeatedly

**问题描述：**

standalone 模式下部署，引擎 Pod 启动时报 EI0014: IP is used repeatedly 错误。

**原因分析：**

standalone 模式下，store 进程与引擎 worker 共用同一 NPU，合并 ranktable 时出现重复的 `device_ip`。这是由于未配置 `ASCEND_LOCAL_COMM_RES={"version":"1.3"}` 导致 ascend_transport 未按 v1.3 格式生成本地通信资源。

**解决步骤：**

1. 在 `env.json` 的 `motor_engine_prefill_env` 和 `motor_engine_decode_env` 中确认已配置：

   ```json
   "ASCEND_LOCAL_COMM_RES": "{\"version\":\"1.3\"}"
   ```

2. 重新部署服务。

### KV 传输失败，引擎间无法建连

**问题描述：**

Prefill 和 Decode 引擎之间无法完成 KV 传输，日志中无传输记录。

**原因分析：**

未配置 `HCCL_INTRA_ROCE_ENABLE=1`，HIXL 底层直连传输未使能 RoCE 协议，导致建连失败。

**解决步骤：**

1. 在 `env.json` 的 `motor_engine_prefill_env` 和 `motor_engine_decode_env` 中确认已配置：

   ```json
   "HCCL_INTRA_ROCE_ENABLE": "1"
   ```

2. 确认该变量配置在 `env.json` 中随部署下发到引擎 Pod，而非仅在部署节点 shell 中 export。
3. 重新部署服务。

### 部署时报 eviction_high_watermark_ratio 或 eviction_ratio 缺失

**问题描述：**

使用 deploy.py 部署 Mooncake 后端时，报错提示 `eviction_high_watermark_ratio` 或 `eviction_ratio` 缺失。

**原因分析：**

deploy.py 对 Mooncake 后端强制校验这两个参数，必须显式配置，即使有默认值也无法省略。

**解决步骤：**

1. 在 `kv_cache_store_config` 中显式配置：

   ```json
   "eviction_high_watermark_ratio": 0.9,
   "eviction_ratio": 0.1
   ```

2. 建议值分别为 0.9 和 0.1，可根据实际场景调整。
3. 重新执行部署。

### 短请求验证时未观测到入池行为

**问题描述：**

使用短 prompt（< 128 token）验证池化功能时，mooncake_master 侧未观测到 `PutStart` 和 `Keys` 指标。

**原因分析：**

`AscendStoreConnector` 按 128 token 的 chunk 粒度做入池判定（`can_save`），只有 token 数达到一个完整 chunk 时才真正触发存池 put。短请求（prompt < 128 token）不会入池，也不会产生 put 流量，这是 vllm-ascend 的设计行为，而非故障。

**解决步骤：**

1. 使用长 prompt（≥ 128 token，建议 500+ token）发起请求。
2. 通过 master 侧观测 `PutStart`/`Keys` 等指标确认 block 确实入池：

   ```bash
   curl http://<master-ip>:<master-port>/metrics | grep -E "PutStart|Keys"
   ```
