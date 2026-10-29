# 950系列服务器跨超节点场景memcache池化后端服务部署指导

本文按组网分成三章。节点名、IP、网口、卡号、hugepage 和 hixlep 目录都换成现场自己的值。MemCache 后端的特性说明见 [MemCache 后端特性](memcache.md)。

## 跨超节点场景（基于1825网卡进行参数面数据传输）

Ascend 950PR&950DT系列产品在跨超节点场景下，用 1825 网卡做参数面数据传输时，在自己的部署脚本里按下面的步骤配置池化服务。节点名、IP、网口、卡号、hugepage 和 hixlep 目录都换成现场自己的值。

这套改法不支持单机多容器。`deploy.py` 和 `delete.sh` 不用改。只用 Docker 时，按 [单容器部署指南](../../../deployment/docker/single_container.md) 做。

### 修改 /etc/hixlep

使用 RoCE 时，在宿主机 `/etc/hixlep` 准备端点配置文件，并在 Prefill、Decode 两侧的 `kv_connector_extra_config` 中设置 `ascend_local_comm_res_path`。两侧 DP 为 4、TP 为 1 时示例如下。把这些字段合并进现有的 `--kv-transfer-config`，并行度按现场部署修改。

```json
{
    "kv_connector_extra_config": {
        "prefill": {
            "dp_size": 4,
            "tp_size": 1
        },
        "decode": {
            "dp_size": 4,
            "tp_size": 1
        },
        "ascend_local_comm_res_path": "/etc/hixlep"
    }
}
```

PD 分离同时通过 MultiConnector 做 KV 池化时，把 `ascend_local_comm_res_path` 写在外层 MultiConnector 的 `kv_connector_extra_config` 中，与 `connectors` 列表同级。不要写进子 connector 的 `kv_connector_extra_config`。worker 从外层 KV 传输配置读取端点文件。Prefill、Decode 的并行度仍写在 Mooncake P/D 子 connector 的配置里。

宿主机上每张实际使用的物理 NPU 各建一个端点文件，文件名为 `/etc/hixlep/ub_endpoint_npu_<物理NPU编号>.json`。后缀是宿主机物理 NPU 编号，不是 DP rank，也不是可见设备中的序号。文件内容示例：

```json
{
    "version": "1.3",
    "net_instance_id": "superpod_0",
    "endpoint_list": [
        {
            "protocol": "roce",
            "comm_id": "<宿主机RoCE IP>",
            "placement": "host"
        }
    ]
}
```

把 `<宿主机RoCE IP>` 换成该节点实际的 RoCE IP。每个容器内，端点文件必须出现在所配置的路径下。

### 修改 boot.sh

容器默认 memlock 只有 64MB。Prefill 打开 `MF_HYBM_RDMA_SWAP_SPACE_SIZE=2048` 时，要 pin 大约 2GB 的 RoCE swap MR。

  1. 进入启动脚本目录并打开 `boot.sh`。

      ```bash
      cd examples/deployer/startup/
      vim boot.sh
      ```

  2. 在 engine 角色（`encode`、`prefill`、`decode`、`union`）执行 `engine.sh` 之前增加 memlock，并保存。

      ```bash
      ulimit -l unlimited
      ```

      改完后在引擎容器里执行 `ulimit -l`，应看到 `unlimited`。

  3. 容器使用 host 网络时，宿主机可能访问不到 `kube-dns` 的 ClusterIP，Decode 会因域名解析失败起不来。先核对集群 DNS 和路由。确认需要规避时，再改 `examples/deployer/startup/boot.sh`。

      只改推理角色这一段，加在 `source "$SCRIPT_DIR/engine.sh"` 前面。`controller`、`coordinator`、`kv_store`、`render` 不用改。

      ```bash
      "encode"|"prefill"|"decode"|"union")
          # 将 <CoreDNS Pod IP> 换成下面命令查到的 IP
          sed -i '1inameserver <CoreDNS Pod IP>' /etc/resolv.conf
          source "$SCRIPT_DIR/engine.sh"
          ;;
      ```

      CoreDNS Pod 的 IP可以通过以下命令查询：

      ```bash
      kubectl get pods -n kube-system -o wide | grep coredns
      ```

### 修改 memcache conf

`device_sdma` 在 Ascend 950PR&950DT系列产品上会直接拒绝，报 `A5 or x86 not support batch extend copy`。`device_urma` 对不上当前 host RoCE。把 LocalService 协议改成 `device_rdma`。这一步只改下面两份 conf。`startup/common.sh` 保持不动，不要在启动脚本里再写一段命令去替换协议。

  1. 打开两份已经存在的 conf。

      ```bash
      cd examples/deployer/startup/roles/kv_store_backends/memcache/
      vim mmc-local-inprocess.conf
      vim mmc-local-standalone.conf
      ```

  2. 搜索协议项，手工改成 `device_rdma` 并保存。不要整文件贴出替换。

  3. inprocess 模板里每个 worker 的 DRAM 从 `0GB` 改成现场池大小。这套例子是 `100GB`。

      >[!NOTE]说明
      > `user_config.json` 里的 `kv_cache_store_config.port` 是池化服务的端口。memcache conf 里的 meta 服务端口要写成同一个数。只改其中一个时，引擎找不到池化服务。

### 修改 infer_service_template.yaml

挂载、host 网络、大页和引擎容器特权都改这份模板。不要改 `lib/generator/engine.py`。

  1. 进入模板目录并打开文件。

      ```bash
      cd examples/deployer/yaml_template/
      vim infer_service_template.yaml
      ```

  2. 搜索 `name: prefill` 和 `name: decode`，在引擎容器的 `securityContext` 中打开特权。否则 TsdOpen 看不见 host RoCE 设备。

      ```yaml
      securityContext:
        privileged: true                 # 新增
        allowPrivilegeEscalation: true   # 新增
      ```

      >[!NOTE]说明
      > 这套跨节点现场不要设置 `ASCEND_GLOBAL_RESOURCE_CONFIG`。设成 `roce:device` 会滤掉 host 侧 `roce:host` 端点。

  3. 在 Prefill、Decode 的 `volumeMounts` 和 `volumes` 里成对增加下面两项，不要再加其他 RoCE 设备目录。

      ```yaml
      volumeMounts:
        - name: hixlep-roce
          mountPath: /path/to/hixlep          # 现场 hixlep 目录，换成自己的路径
        - name: dev-infiniband
          mountPath: /dev/infiniband
      volumes:
        - name: hixlep-roce
          hostPath:
            path: /path/to/hixlep             # 与 mountPath 一致
            type: DirectoryOrCreate
        - name: dev-infiniband
          hostPath:
            path: /dev/infiniband
            type: Directory
      ```

      Prefill 读的是这个现场 hixlep 目录，不是 `/etc/hixlep`。没有该目录，或者没有整棵 `/home`，Prefill 读不到端点文件。

  4. Encode、Prefill、Decode、Union 再成对增加下面这些挂载。`hccn-tool-bin` 在宿主机上是文件时，`hostPath.type` 留空；是目录再写 `Directory`。

      ```yaml
      volumeMounts:
        - name: host-home
          mountPath: /home
        - name: ascend-firmware
          mountPath: /usr/local/Ascend/firmware
          readOnly: true
        - name: host-sbin
          mountPath: /usr/local/sbin
        - name: npu-slog
          mountPath: /usr/slog
        - name: root-host
          mountPath: /root/host
        - name: hccn-tool-bin
          mountPath: /usr/local/Ascend/driver/tools/hccn_tool
      volumes:
        - name: host-home
          hostPath:
            path: /home
        - name: ascend-firmware
          hostPath:
            path: /usr/local/Ascend/firmware
        - name: host-sbin
          hostPath:
            path: /usr/local/sbin
        - name: npu-slog
          hostPath:
            path: /var/log/npu
        - name: root-host
          hostPath:
            path: /root/host
        - name: hccn-tool-bin
          hostPath:
            path: /usr/local/Ascend/driver/tools/hccn_tool
      ```

      >[!NOTE]说明
      > `lib/constant.py` 的 `A5_HOST_PATH_VOLUMES_HOST_NIC` 里去掉重复的 `npu-smi-bin`。引擎模板已经用 volume 名 `npu-smi` 挂了同一个路径，两个名字挂到同一 `mountPath` 会被 API 拒绝。

  5. 给 Prefill 和 Decode 的 Pod 打开 host 网络。InferServiceSet 加在角色的 Pod spec 上，只增加这两个字段：

      ```yaml
      spec:
        template:
          roles:
            - name: prefill
              spec:
                template:
                  spec:
                    hostNetwork: true
                    dnsPolicy: ClusterFirstWithHostNet
            - name: decode
              spec:
                template:
                  spec:
                    hostNetwork: true
                    dnsPolicy: ClusterFirstWithHostNet
      ```

      >[!NOTE]说明
      > 若 `user_config.json` 中配置了 `"deploy_mode": "multi_deployment"`，host 网络改 `engine_template.yaml`，字段放在 `spec.template.spec` 下。不能把普通 Deployment 的层级直接套在 InferServiceSet 顶层。`dnsPolicy` 不会自动修复节点到 DNS 的路由。

  6. 搜索 `name: prefill`。只给 Prefill 加大页。Decode 只走 Mooncake、没有单独的大页实测需求时，不加这项申请，避免大页额度申请过多导致 Pod 无法调度。request 和 limit 必须相等。`416Gi` 是示例，改之前先看节点 allocatable。

      ```yaml
      resources:
        requests:
          memory: "64Gi"
          cpu: "16"
          hugepages-2Mi: 416Gi          # 该字段需要修改，只加在 Prefill
          huawei.com/Ascend910: 1
        limits:
          hugepages-2Mi: 416Gi          # 与 request 相同
          huawei.com/Ascend910: 1
      volumeMounts:
        - name: hugepage-2mi
          mountPath: /hugepages-2Mi
      volumes:
        - name: hugepage-2mi
          emptyDir:
            medium: HugePages-2Mi
      ```

### 修改 env.json 和 user_config.json

现场目录是 `model_configs/vllm/dsv4_roce_pd/`。

  1. 打开 `env.json`，在公共项写入 `MMC_A5_INPROCESS_DRAM_SIZE=100GB`。

  2. 只在 Prefill 写入下面三项，并保存。Decode 不配置这三项。

      ```text
      MF_HYBM_RDMA_USE_HCOMM=1
      MF_HYBM_RDMA_SWAP_SPACE_SIZE=2048
      MF_HYBM_RDMA_FORCE_UNREGISTERED=1
      ```

  3. 打开 `user_config.json`，按角色改 KV connector 和 hixlep 路径。

      - Prefill 和 Decode 使用相同的池化 connector：MooncakeHybrid 加 AscendStore memcache。
      - 两边的 hixlep 都指向本节点上的端点目录，示例路径是 `/path/to/hixlep`。

## 跨超节点场景（基于UB_RTP协议进行参数面数据传输）

该场景基于 UB_RTP 协议做参数面数据传输。在 PD 分离部署指导的基础上，额外修改以下内容，即可完成部署。

  1. 修改 `user_config.json`。`motor_deploy_config.hardware_type` 设为 `Ascend950-Pod16`。

  2. 修改 `env.json`。在 `motor_common_env`、`motor_engine_prefill_env`、`motor_engine_decode_env` 中写入：

      ```text
      ASCEND_GLOBAL_RESOURCE_CONFIG={"comm_resource_config.protocol_desc":["ub_rtp:device"]}
      ASCEND_LOCAL_COMM_RES={"version":"1.3"}
      ```

      >[!NOTE]说明
      > 该场景会自动开启推理引擎所在容器的 root 权限，并额外挂载 `/dev/ummu`、`/dev/uburma` 两条宿主机路径。同时为引擎 Pod 打开 host 网络（`hostNetwork`）。暂不支持单机多容器场景。

## 跨超节点场景（基于 UBoE 协议进行参数面数据传输）

在 PD 分离部署指导的基础上，额外修改以下内容，即可完成部署。

  1. 修改 `user_config.json`。`motor_deploy_config.hardware_type` 设为 `Ascend950`。

  2. 修改 `env.json`。在 `motor_common_env`、`motor_engine_prefill_env`、`motor_engine_decode_env` 中写入：

      ```text
      ASCEND_GLOBAL_RESOURCE_CONFIG={"comm_resource_config.protocol_desc":["uboe:device"]}
      ASCEND_LOCAL_COMM_RES={"version":"1.3"}
      ```

      >[!NOTE]说明
      > 该场景会自动开启推理引擎所在容器的 root 权限，并额外挂载 `/dev/ummu`、`/dev/uburma` 两条宿主机路径。
