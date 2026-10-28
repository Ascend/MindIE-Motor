# Modifying the Deployer for Complex Networking

On 950PR&950DT products, when host RoCE is used for HYBM pooling and for cross-node Prefill and Decode, the following changes must be made in **your own deployment copy** of `examples/deployer`, relative to the current `master`. The changes are bound to the site NICs, DNS, card IDs, and huge pages, so they are not merged into the trunk directly.

The IP addresses, node names, card IDs, huge page sizes, and hixlep paths below are all from one site example (Prefill on `motor-master-109`, Decode on `motor-worker-104`, namespace `mindie-motor-dsv4-roce`). Replace them with your own values in another environment.

Sections 1 to 12 describe this cross-node host RoCE setup. When Prefill and Decode run together on the same machine and the container is not privileged, use section 13 instead of sections 3, 6, and 8; for port configuration of multiple P/D instances on the same machine, see section 14.

`deploy.py` and `delete.sh` do not need to be changed.

## 1. Unlocking memlock

By default, a container has only 64MB of memlock. When Prefill sets `MF_HYBM_RDMA_SWAP_SPACE_SIZE=2048`, about 2GB of RoCE swap MR must be pinned. If this memory cannot be locked, HCOMM / swap cannot start, and subsequent puts fail.

In `examples/deployer/startup/boot.sh`, add the following before the engine roles (`encode`, `prefill`, `decode`, `union`) execute `engine.sh`:

```bash
ulimit -l unlimited
echo "memlock=$(ulimit -l)"
```

After the change, run `ulimit -l` in the engine container; it should return `unlimited`.

## 2. Changing the Memcache Protocol to device_rdma

On 950PR&950DT products, the current `master` sets the LocalService protocol to `device_urma` (earlier templates default to `device_sdma`). This pooling uses HYBM RoCE, not UB.

- `device_sdma` is rejected outright on 950PR&950DT products, reporting `A5 or x86 not support batch extend copy`.

- `device_urma` does not match the current host RoCE.

In `sync_mmc_local_config` in `examples/deployer/startup/common.sh`, the branch for this product changes the protocol rewrite from `device_urma` to `device_rdma`:

```bash
sed -E -i.bak "s|^(ock\\.mmc\\.local_service\\.protocol)[[:space:]]*=.*|\\1 = device_rdma|" "$_dst"
```

The templates may be changed to `device_urma` first; at runtime they are still overwritten to `device_rdma` by the sed command above. Both templates must have their default value changed:

- `startup/roles/kv_store_backends/memcache/mmc-local-inprocess.conf`

- `startup/roles/kv_store_backends/memcache/mmc-local-standalone.conf`

```text
ock.mmc.local_service.protocol = device_urma
```

The inprocess template also requires changing the DRAM of each worker from `0GB` to the site's pool size. In this example it is `100GB`, and `MMC_A5_INPROCESS_DRAM_SIZE=100GB` is written in `env.json`. With `0GB`, `hbm{0},dram{0}` is created, and there is no place to put prefix blocks.

In the same function, if `KV_CACHE_STORE_PORT` is set, change the port of `ock.mmc.meta_service_url` in the conf to this port. `kv_cache_store_config.port` in `user_config.json` only affects the MetaService listener, while LocalService still reads the conf; if the two are inconsistent, the pool cannot connect.

## 3. Adding Privileges to the Engine Container

The current `master` grants privileges to the engine only under the host-nic overlay of ASCEND_GLOBAL_RESOURCE_CONFIG (`uboe` / `roce` / `ub_rtp:device`). To align with a colleague's Docker `--privileged`, always enable privileges for the engine Pods on 950PR&950DT products; otherwise TsdOpen cannot see the host RoCE devices.

In `apply_a5_engine_pod_config` in `examples/deployer/lib/generator/engine.py`, in the branch for this product, after appending the hostPath and before checking the overlay, add the following to the container:

```python
sec = container.setdefault(C.SECURITY_CONTEXT, {})
sec[C.PRIVILEGED] = True
sec["allowPrivilegeEscalation"] = True
```

Do not set `ASCEND_GLOBAL_RESOURCE_CONFIG` in this cross-node scenario. Setting it to `roce:device` filters out the host-side `roce:host` endpoint. The Prefill TransferEngine then fails, and Decode cannot find `/etc/hixlep/ub_endpoint_npu_*.json` either. The single-node non-privileged approach is described in section 13; do not copy `max_active_channels` from there to this host endpoint.

## 4. Additional Mounts

### 4.1 hostPath Injected by the Generator

In `A5_HOST_PATH_VOLUMES` in `examples/deployer/lib/constant.py`, keep the original `/etc/hixlep` (volume name `hixlep`), and add:

| Volume Name | Host Path | type |
|-----------|------------|------|
| `hixlep-roce` | The hixlep directory on the site; in this example, `/home/z00931220/hixlep` | `DirectoryOrCreate` |
| `dev-infiniband` | `/dev/infiniband` | `Directory` |
| `dev-ummu` | `/dev/ummu` | `Directory` |
| `dev-uburma` | `/dev/uburma` | `Directory` |

Prefill reads `/home/.../hixlep`, not `/etc/hixlep`. Without `hixlep-roce`, or without the entire `/home` tree, Prefill cannot read the endpoint file.

Remove the duplicate `npu-smi-bin` from `A5_HOST_PATH_VOLUMES_HOST_NIC`. The engine template already mounts the same path with the volume name `npu-smi`, and two names mounted to the same `mountPath` are rejected by the API.

Do not add `/dev/hisi_hdc` or `/dev/davinci_manager`. Being able to see the devices does not solve the native `RaGetIfNum count: 0`.

### 4.2 hostPath in the Engine Template

In `examples/deployer/yaml_template/infer_service_template.yaml`, add the following mounts to Encode, Prefill, Decode, and Union. Write `volumeMounts` and `volumes` in pairs.

| Volume Name | Host | Container | Description |
|-----------|--------|------|------|
| `host-home` | `/home` | `/home` | Prefill reads hixlep |
| `ascend-firmware` | `/usr/local/Ascend/firmware` | Same path, read-only | |
| `host-sbin` | `/usr/local/sbin` | Same path | |
| `npu-slog` | `/var/log/npu` | `/usr/slog` | |
| `root-host` | `/root/host` | Same path | |
| `hccn-tool-bin` | `/usr/local/Ascend/driver/tools/hccn_tool` | Same path | |

If `hccn-tool-bin` is a file on the host, leave `hostPath.type` empty; if it is a directory, write `Directory`.

## 5. Using the Host Network

RoCE on the host network requires visibility of the host NICs. In `examples/deployer/yaml_template/infer_service_template.yaml`, add `hostNetwork: true` and `dnsPolicy: ClusterFirstWithHostNet` to the **Pod spec** of Prefill and Decode respectively. The hierarchy of InferServiceSet is as follows; add only these two fields, and keep the existing role definitions, containers, scheduling, and mount configuration:

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

For an ordinary Deployment/StatefulSet (including `engine_template.yaml` of `multi_deployment`), the fields go in `spec.template.spec`:

```yaml
spec:
  template:
    spec:
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
```

Both forms set the PodSpec; never apply the hierarchy of an ordinary Deployment directly at the top level of InferServiceSet. `dnsPolicy` makes host-network Pods use the cluster DNS; it does not automatically fix the routing from the node to the DNS. The port adaptation switch `MOTOR_INSTANCE_PORTS_ENABLE` does not automatically enable the host network either.

For multiple P/D instances running on the same host under the host network, see [Port Configuration for Multiple P/D Instances on the Same Machine](#14-port-configuration-for-multiple-pd-instances-on-the-same-machine).

If hostNetwork Pods in the site cannot access the ClusterIP of kube-dns (`10.96.0.10` in this example), Decode may fail to start as a result. Check the cluster DNS and routing first; if the workaround in this example is really needed, replace that ClusterIP in `/etc/resolv.conf` with the IP of a CoreDNS Pod in this cluster and put it first, in the engine branch of `boot.sh`.

Look up the CoreDNS Pod IP first; do not copy the address in the example:

```bash
kubectl get pods -n kube-system -o wide | grep coredns
```

On site 109 the addresses are `10.244.113.66` and `10.244.113.67`. The rewrite only affects the engine process; the kubelet liveness check does not source `boot.sh`.

## 6. Using 2Mi Huge Pages for Prefill

Add huge pages to Prefill only. Decode usually needs none when it uses only Mooncake. K8s requires the huge page request and limit to be equal.

The Prefill container in `infer_service_template.yaml`:

```yaml
resources:
  requests:
    memory: "64Gi"
    cpu: "16"
    hugepages-2Mi: 416Gi
    huawei.com/Ascend910: 1
  limits:
    hugepages-2Mi: 416Gi
    huawei.com/Ascend910: 1
volumeMounts:
  - name: dshm
    mountPath: /dev/shm
  - name: hugepage-2mi
    mountPath: /hugepages-2Mi
volumes:
  - name: dshm
    emptyDir:
      medium: Memory
      sizeLimit: 4Gi
  - name: hugepage-2mi
    emptyDir:
      medium: HugePages-2Mi
```

`416Gi` is the example of this machine. Before changing it, check the huge pages on the node and whether K8s already reports `hugepages-2Mi` as allocatable. Writing it in the template does not mean the scheduler accepts it. The site OS had 2Mi huge pages, but the allocatable value was 0 at that time, and the Pod stayed Pending.

```bash
kubectl describe node <node-name> | grep -A2 hugepages
```

## 7. Removing the HTTP Proxy from the Image

The image ENV includes `HTTP_PROXY`. If `set_common_env` sets it to an empty string, the variable still exists and the libraries still use the proxy.

In `startup/boot.sh`, after sourcing `common.sh` and before the role branches:

```bash
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
```

The kubelet executes `probe/probe.sh` directly without sourcing `boot.sh`, so `probe.sh` must unset these four variables as well.

Add `trust_env=False` to the httpx client in `probe/probe.py` to prevent the proxy from intercepting probes sent to the Pod IP or Service:

```python
client_kwargs = {"headers": headers, "timeout": timeout, "trust_env": False}
```

In `motor_common_env` of `env.json`, set the four proxy variables to empty strings, and set `no_proxy` / `NO_PROXY` to the local host, the node management IP, and the service IP.

## 8. Overriding Visible Devices by Physical Card ID

A privileged container can see all NPUs on the node. When `_calc_visible_device_ids` in the image does not read `ASCEND_RT_VISIBLE_DEVICES`, Prefill occupies the cards of the sibling process by logical card `0,1,2,3`.

Do not merge the whole `service.py` into the trunk. Put the same patch on the local disk of both nodes, for example `/mnt/motor_patch/service.py` (`/mnt` is not a shared disk, so both 109 and 104 need it). In the engine branch of `boot.sh`, before sourcing `engine.sh`:

```bash
MOTOR_SERVICE_PATCH="/mnt/motor_patch/service.py"
MOTOR_SERVICE_DEST="/usr/local/python3.11.10/lib/python3.11/site-packages/motor/node_manager/core/services/native_engine/service.py"
if [ -f "$MOTOR_SERVICE_PATCH" ]; then
    cp -f "$MOTOR_SERVICE_PATCH" "$MOTOR_SERVICE_DEST"
    rm -f "$(dirname "$MOTOR_SERVICE_DEST")/__pycache__/service."*.pyc
fi
```

The site-packages path in the image varies with the Python version, so confirm the actual location of `service.py` in the container first.

The patch changes `NativeEngineService._calc_visible_device_ids`: it reads `ASCEND_RT_VISIBLE_DEVICES` first and falls back to `ASCEND_VISIBLE_DEVICES`, and replaces the computed logical card IDs with this physical card list. On the site, Prefill is fixed to `0,2,4,6` and Decode to `1,3,5,7`, consistent with `ASCEND_RT_VISIBLE_DEVICES` in `env.json`. When you change card positions, change both places.

Do not write `ASCEND_VISIBLE_DEVICES=0..n-1` in the Docker bypass. When a privileged container sees all cards, the logical numbering collides with the Prefill on the other side. In `docker_deploy.py`, change it to:

```text
-e ASCEND_RT_VISIBLE_DEVICES=<physical card ID list>
```

## 9. Probe Timeouts

Host networking and RoCE initialization are slower. Change the `timeoutSeconds` of the startup probe to `30`:

- The startupProbe of Encode, Prefill, and Decode in `yaml_template/infer_service_template.yaml`

- `yaml_template/controller_template.yaml`

- `yaml_template/coordinator_template.yaml`

- `yaml_template/kv_cache_store_template.yaml` (the value here was `3`)

If the kv-store must run on a specific machine, add `nodeSelector` to its Pod. The example is `kubernetes.io/hostname: motor-master-109`. Do not write this host name into the generic template and merge it into the trunk.

## 10. Additional Items for Docker-Only Deployment

The K8s `deploy.py` does not use these files. When only `docker_deploy.py` is used:

- `ENTER_DOCKER_RUN_A5` in `lib/constant.py`: change `/dev/ummu` and `/dev/uburma` from `--device` to `-v` binds; bind an extra copy of `native_engine/service.py` when the overlay is needed.

- `ENTER_DOCKER_RUN_KVS`: remove `-v /driver:/driver`.

- `docker_deploy.py`: remove `docker run -t` and keep only `-i`. When `isatty()` is true, CANN slog is printed to the startup terminal; K8s Pods have no TTY by default, so this problem does not occur.

- `lib/in_place_run.py`: do not open a PTY for `start_motor`. Use a pipe and set `PYTHONUNBUFFERED=1`.

## 11. Matching env.json and user_config.json

These two files are not in `examples/deployer`; they are used together with the scripts above. The site directory is `model_configs/vllm/dsv4_roce_pd/`.

`env.json`:

- Common: `MMC_A5_INPROCESS_DRAM_SIZE=100GB`, and the proxy is cleared as described in section 7.

- Prefill: `ASCEND_RT_VISIBLE_DEVICES=0,2,4,6`, plus `MF_HYBM_RDMA_USE_HCOMM=1`, `MF_HYBM_RDMA_SWAP_SPACE_SIZE=2048`, and `MF_HYBM_RDMA_FORCE_UNREGISTERED=1`.

- Decode: `ASCEND_RT_VISIBLE_DEVICES=1,3,5,7`; do not add the three HYBM switches.

- Do not set `ASCEND_GLOBAL_RESOURCE_CONFIG`. The single-node non-privileged approach is described in section 13.

Fill in `user_config.json` according to your own cluster. This example is:

- Namespace `mindie-motor-dsv4-roce`

- One instance each for Prefill and Decode, 4 cards per Pod; Prefill pinned to 109 and Decode pinned to 104

- Prefill enables prefix cache, the KV connector uses MooncakeHybrid plus AscendStore memcache, and hixlep points to `/home/z00931220/hixlep`

- Decode disables prefix cache, keeps only MooncakeHybrid, and hixlep points to `/etc/hixlep`

- kv_cache_store uses memcache inprocess with port `50088`, consistent with `KV_CACHE_STORE_PORT` and the meta URL in the conf

Change `name_space` in `log_collect/log_config.ini` to the namespace of this deployment. It is a filter condition of the collection script, not part of the deployment logic.

## 12. How to Verify After Deployment

Deploy only from the machine that has the cluster management kubeconfig installed. Do not touch other people's namespaces or containers that already occupy cards.

```bash
cd examples/deployer
bash delete.sh <this namespace>
kubectl delete ns <this namespace> --wait=true
python3 deploy.py --config_dir <this user_config directory>
```

Send requests to the NodePort of the Coordinator. When the image has a proxy, add `--noproxy '*'` to curl.

To check whether pooling takes effect, look at `kvpool hit tokens` in the Prefill logs instead of judging by the Prefix cache hit rate.

## 13. Single-Node Non-Privileged RoCE

Use this section when Prefill and Decode both run on the same machine and the container cannot be privileged. The following is written for a deployment that already works on `worker-112`. The host name, IP addresses, NICs, endpoint directory, ports, and library file names are all examples of this machine; replace them with your own values in another environment.

The differences between this section and the preceding ones:

- The container uses `privileged: false` and `allowPrivilegeEscalation: false`, and adds only `NET_ADMIN` and `IPC_LOCK`.

- The entire `/dev/infiniband` tree is not mounted into the engine. The device plugin registers `devic.es/roce`, and Prefill and Decode each request 1.

- `service.py` is not overwritten. The startup command rewrites the physical cards allocated by the scheduler into logical cards `0..N-1`.

- Both Prefill and Decode request `hugepages-2Mi: 16Gi`.

- `ASCEND_GLOBAL_RESOURCE_CONFIG` contains only `{"comm_resource_config.max_active_channels":2}`. Do not write it as `roce:device`.

- Prefill, Decode, Controller, Coordinator, and KV Store are placed on the same node. With `hostNetwork` on the same machine, the ports on both sides must be different.

### 13.1 Providing RoCE Devices with a Device Plugin

Deploy generic-device-plugin on the service node with the resource name `roce`; the name requested in the Pod is `devic.es/roce`. This machine registers 2 shares, each mapping the same group of devices: `/dev/infiniband/uverbs0` to `uverbs7` and `/dev/infiniband/rdma_cm`. Prefill and Decode each take 1 share. These device nodes must actually exist on the node; do not put non-existent ones into the path list.

The plugin is placed in `kube-system` with `nodeSelector` pinned to the service node, for example `kubernetes.io/hostname: worker-112`. The plugin itself uses `privileged: false` and `allowPrivilegeEscalation: false`, and mounts `/var/lib/kubelet/device-plugins` and `/dev/infiniband`. Import the image to that node first and set `imagePullPolicy` to `Never`. The image of this machine is `squat/generic-device-plugin:dts-c5e0633438d1`.

The core of the plugin parameters is:

```text
--device {"name":"roce","groups":[{"count":2,"paths":["/dev/infiniband/uverbs0","/dev/infiniband/uverbs1","/dev/infiniband/uverbs2","/dev/infiniband/uverbs3","/dev/infiniband/uverbs4","/dev/infiniband/uverbs5","/dev/infiniband/uverbs6","/dev/infiniband/uverbs7","/dev/infiniband/rdma_cm"]}]}
```

After deployment, confirm that `devic.es/roce` in the allocatable resources of the node is at least 2. Otherwise one of the two engine Pods stays Pending.

### 13.2 Modifying the Engine Pod

Modify Prefill and Decode in `examples/deployer/yaml_template/infer_service_template.yaml` of the deployment copy. If the roles are split into other template files, modify those files, and do not merge them into the trunk after the modification.

Set the Pod specs of Prefill and Decode according to the resource hierarchy in section 5 and keep them:

```yaml
hostNetwork: true
dnsPolicy: ClusterFirstWithHostNet
```

If the ClusterIP of kube-dns is unreachable, change the `resolv.conf` of the engine to the CoreDNS Pod IP as described in section 5. The startup command of this machine does not include this rewrite.

Container security context and resources:

```yaml
securityContext:
  privileged: false
  allowPrivilegeEscalation: false
  capabilities:
    add:
      - NET_ADMIN
      - IPC_LOCK
resources:
  requests:
    hugepages-2Mi: 16Gi
    devic.es/roce: 1
  limits:
    hugepages-2Mi: 16Gi
    devic.es/roce: 1
```

`16Gi` is the example of this machine. The huge page request and limit must be equal. Check the node allocatable resources first; writing them in the template does not mean the scheduler accepts them.

Additional mounts. The three provider files are read-only, with `hostPath.type` set to `File`:

| Volume Name | Host | Container |
|-----------|--------|------|
| `roce-provider-0` | `/etc/libibverbs.d/hrn5.driver` | Same path, read-only |
| `roce-provider-1` | `/usr/lib64/libhrn5-rdmav34.so` | Same path, read-only |
| `roce-provider-2` | `/usr/lib64/libibv_extend.so.4` | Same path, read-only |
| `dev-ummu` | `/dev/ummu` | `/dev/ummu` |
| `dev-uburma` | `/dev/uburma` | `/dev/uburma` |

Library file names vary with the driver. Confirm the actual paths of the provider and the two shared objects on the host first.

Before the startup command of Prefill and Decode enters `boot.sh`, it uses the physical cards that the scheduler writes into `ASCEND_VISIBLE_DEVICES` to do two things: generate the logical cards, and make the parent process carry the endpoint of the first physical card. `export` must run in the same shell as `boot.sh`, and the template uses `source`. With four cards, the logical cards are `0,1,2,3`. Replace the endpoint directory with your own hixlep directory; the protocol field in the file is `roce`.

```bash
allocated_devices="${ASCEND_VISIBLE_DEVICES//npu-/}"
if ! [[ "$allocated_devices" =~ ^[0-9]+(,[0-9]+)*$ ]]; then
    echo "Invalid scheduler NPU allocation" >&2
    exit 2
fi
IFS=, read -ra allocated_list <<< "$allocated_devices"
export ASCEND_RT_VISIBLE_DEVICES="$(seq -s, 0 $((${#allocated_list[@]}-1)))"
export ASCEND_LOCAL_COMM_RES="$(cat /mnt/share/y00943510/hixlep/ub_endpoint_npu_${allocated_list[0]}.json)"
export LD_LIBRARY_PATH=/usr/local/Ascend/driver/lib64/driver:/usr/local/Ascend/driver/lib64/common:$LD_LIBRARY_PATH
source /mnt/configmap/boot.sh
```

Do not install the card selection fix package in `boot.sh`; use the Motor in the image. Do not copy `service.py` into site-packages as described in section 8.

Add the `nodeSelector` of the same machine to the Pods of Controller, Coordinator, and KV Store. The example is `kubernetes.io/hostname: worker-112`. The nodes of Prefill and Decode are written in `prefill_node_selector` and `decode_node_selector` in `user_config.json`; in this example both are `worker-112`.

### 13.3 Separating Ports on the Same Machine

In `user_config.json`, Prefill and Decode each have 1 instance, 1 Pod, and 4 cards, with `data_parallel_size` set to 4 and `tensor_parallel_size` set to 1. Set `hardware_type` to `Ascend950`. Fill in the image according to the on-site 950PR&950DT series product image.

With `hostNetwork` on the same machine, the following ports must not be shared between the two sides:

| Item | Prefill | Decode |
|----|---------|--------|
| `data_parallel_rpc_port` | `19000` | `19001` |
| `node_manager_port` | `11026` | `12026` |
| `base_port` | `11000` | `12000` |
| `ASCEND_BASE_PORT` | `22000` | `24000` |
| `HIXL_LISTEN_PORT` | `26666` | `26766` |
| Mooncake `kv_port` | `20001` | `20011` |

`ASCEND_BASE_PORT` and `HIXL_LISTEN_PORT` are written in the corresponding roles of `env.json`, and the rest are written in `user_config.json`. `motor_nodemanger_config` is the actual spelling of the existing configuration key name.

In this example, neither P nor D has `speculative-config`, and `no-enable-prefix-caching` is true. The KV connector uses MooncakeHybrid plus AscendStore, and `ascend_local_comm_res_path` points to the endpoint directory, for example `/mnt/share/y00943510/hixlep`.

`kv_cache_store_config` uses mooncake standalone: the example of `global_segment_size` is `10GB`, that of `local_buffer_size` is `1GB`, and `default_kv_lease_ttl` is explicitly written as `60000` ms. The default lease of the deployer is `11000` ms, which is inconsistent with this setup if not changed.

Write the following in `env.json` for both Prefill and Decode:

- `HCCL_IF_IP` uses the service address of this machine, for example `141.61.45.112`

- `GLOO_SOCKET_IFNAME`, `HCCL_SOCKET_IFNAME`, and `TP_SOCKET_IFNAME` use the same NIC, for example `enp34s0f1`

- `HCCL_BUFFSIZE=512`, `HCCL_ALGO=level0:fullmesh`

- `HCCL_EXEC_TIMEOUT=204`, `HCCL_CONNECT_TIMEOUT=120`

- `ASCEND_THREAD_POOL_SIZE=1`

- `ASCEND_GLOBAL_LOG_LEVEL=3`

- `MC_STORE_USE_HUGEPAGE=1`

- `ASCEND_GLOBAL_RESOURCE_CONFIG={"comm_resource_config.max_active_channels":2}`

Do not set `ASCEND_GLOBAL_RESOURCE_CONFIG` to `roce:device`.

### 13.4 Enabling Local RoCE Forwarding

When Prefill and Decode run on the same machine, RoCE traffic must leave from the local address and come back to it. This machine sets `net.ipv4.ip_forward` to `1` and prepares a dedicated routing table for each RoCE NIC: a `/32` route that points to the local address through the gateway, plus a policy rule with priority `0` that matches a source address equal to the destination address and packets entering from the local loopback interface. The priority of the system `lookup local` rule is changed from `0` to `1` so that it does not take precedence over these rules. The peer in the same bond is written as a permanent neighbor entry, with the MAC address taken from the site gateway. Set `accept_local=1` on the corresponding interface.

NIC names, addresses, and routing table numbers are all site values. Make the configuration run automatically at boot and when the NIC goes up, so that forwarding is not lost after a restart.

### 13.5 Do Not Mix with the Cross-Node Approach

Do not combine this section with the blanket privileges of section 3, the entire `/dev/infiniband` of section 4, the `416Gi` huge pages for Prefill only of section 6, or the physical card patch of `service.py` in section 8. Those are approaches for cross-node scenarios where the container can see all cards. In this section, cards are allocated by the scheduler, and logical cards are numbered from 0.

## 14. Port Configuration for Multiple P/D Instances on the Same Machine

When multiple Prefill/Decode Pods that use `hostNetwork` run on the same node, all instances share the port space of the host.
Distinguishing only the base ports of Prefill and Decode cannot distinguish P0 and P1 of the same role. This section describes the optional deployment capability that generates ports by instance index.

### 14.1 Scope

- Kubernetes InferServiceSet deployment, using the `ROLE` and `INSTANCE_INDEX` injected by MindCluster.

- One Pod per instance, with the base ports of both P and D explicitly configured.

- The user confirms that the nodes, instance placement, NPUs, RoCE devices, huge pages, and network conditions meet the deployment requirements.

- This feature generates only the instance configuration, and the native Motor deployer and NodeManager continue to be used.

It is disabled by default. An instance composed of multiple Pods, Docker, Slurm, and the mixed deployment of encode and union are not within the scope of this port adaptation; these roles do not enter the P/D conversion branch and still follow the original process. Mixed deployment cannot be applied to this example directly; plan and verify the ports separately.
When multiple InferServiceSets are deployed on the same node, different base ports must also be planned for different services; the instance index is meaningful only within its own service.

### 14.2 Configuration Method

Add the following to `motor_common_env` in `env.json`:

```json
{
  "MOTOR_INSTANCE_PORTS_ENABLE": "1",
  "MOTOR_INSTANCE_PORT_STRIDE": "100",
  "MOTOR_INSTANCE_KV_PORT_STRIDE": "100",
  "MOTOR_INSTANCE_ASCEND_PORT_STRIDE": "4000",
  "MOTOR_INSTANCE_STORE_PORT_STRIDE": "200"
}
```

The four strides apply to the management/API/DP RPC ports, the Connector KV port, the Ascend base port, and the Store HIXL port respectively.
The offset rule is: `actual port of the instance = base port of the current role + INSTANCE_INDEX × stride`.
The default strides are the same as in the example above; if a port or offset is invalid, startup is refused, without truncation or wraparound.

The deployer automatically distributes `startup/instance_config.py` to the ConfigMap. When the feature is enabled, use this in-repository tool; there is no need to share a script in a personal directory through a mount.
`MOTOR_INSTANCE_CONFIG_ADAPTER` can explicitly specify a compatible deployment adapter script; it is an O&M configuration and should point only to trusted files.

Set the following in `motor_nodemanger_config` of P and D in `user_config.json`:

```json
{
  "api_config": {"node_manager_port": 11026},
  "endpoint_config": {"base_port": 11000},
  "port_allocator_config": {"enable": false}
}
```

The example above is for Prefill; Decode needs its own base ports, for example `12026` / `12000`.
Also explicitly set `engine_config.data_parallel_rpc_port` of P and D and the `kv_port` of the Connector in use.
`motor_nodemanger_config` is the actual spelling of the existing configuration key. The explicit instance port scheme requires the port allocator to be disabled; otherwise the conversion is refused.

The base `ASCEND_BASE_PORT` and `HIXL_LISTEN_PORT` must also be set in the environment of P and D respectively.
Port planning must not only keep the starting values in the table different, but also reserve the contiguous port ranges actually used by the engine and the underlying communication.

### 14.3 Single-Node 2P1D Example

On top of the non-privileged RoCE conditions in section 13, Qwen3-8B uses two Ps and one D, with one Pod and one NPU per instance, and TP=DP=PP=1. The two RoCE quotas in section 13 correspond to the original 1P1D, while the three instances in this example need at least three. Configure the host network as described in section 5, and then set the following instance ports:

| Parameter | P0 | P1 | D0 |
| --- | ---: | ---: | ---: |
| NodeManager management port | 11026 | 11126 | 12026 |
| Engine API start port | 11000 | 11100 | 12000 |
| DP RPC | 19000 | 19100 | 19001 |
| `ASCEND_BASE_PORT` | 22000 | 26000 | 24000 |
| `HIXL_LISTEN_PORT` | 26666 | 26866 | 26766 |
| Connector `kv_port` | 30001 | 30101 | 30002 |

Only the base values of P0/D0 are filled in the configuration. The effective values of P1 are calculated from the instance index, and there is no need to manually copy a Prefill configuration.

### 14.4 Configuration Synchronization, Probes, and Exit

1. `common.sh` generates the effective configuration of the current instance from the raw JSON in the ConfigMap.
2. The configuration is written to `$CONFIG_PATH/user_config.json`, and `instance-ports.json` is written to the same directory.
3. Each background synchronization converts the raw JSON again, keeps new fields, and does not accumulate port offsets.
4. `engine.sh` sets the instance communication ports after the role environment is initialized, so that they are not overwritten by the role base values.
5. The probe reads the effective instance configuration from `CONFIG_PATH`.
6. PreStop verifies the role, index, path, and management port of the instance marker, and then accesses only the NodeManager of the current instance.

A failure of the first conversion prevents startup. If a background conversion fails, the last effective configuration is kept and the synchronization loop stops, the error is written to the log, and the configuration must be corrected and the corresponding Pod restarted.
When PreStop encounters a corrupted or mismatched instance marker, it refuses to access other ports in the shared configuration.

When instance conversion is not enabled and no instance marker exists, the original configuration synchronization and the PreStop configuration search order are kept.
`instance-ports.json` is instance runtime state; it should not be used as ConfigMap input, nor copied across instances.

### 14.5 Prerequisites for RoCE and an Independent Store

The port fix does not replace device and network configuration. If P0, P1, and D0 each request `devic.es/roce: 1`, the target node needs at least three idle allocatable quotas.
The quota published by the device plugin is a count of a shared device resource; it does not represent an additional physical NIC, nor bandwidth isolation.

Non-privileged RoCE also requires device access, matching dynamic libraries, network capabilities, and memory locking capabilities configured according to the actual driver/provider.
When an independent Store uses huge pages, the Pod needs matching hugepage requests/limits; setting only `MC_STORE_USE_HUGEPAGE=1` does not request a Kubernetes huge page quota.
Whether local RoCE loopback is available should be tested independently; host policy routing and the gateway are site network configuration.

In the Qwen3-8B verification of one A5/B100 image, failures occurred during large dense KV transfers; after keeping RoCE and the standalone Store and merging the following parameters into the P/D `ASCEND_GLOBAL_RESOURCE_CONFIG`, an independent write, zeroing, read-back, and complete SHA256 verification of about 900MiB passed:

```json
{
  "comm_resource_config.max_active_channels": 2,
  "transfer_config.max_transfer_count_per_batch": 16
}
```

This is a communication library configuration workaround for a tested combination; it is not Motor fixing the underlying communication library, nor a globally recommended default value.
Keep the other necessary parameters in the original JSON, and confirm support, functionality, and performance on the CANN/HIXL version in use.

### 14.6 Acceptance

Run a dry-run of the native deployer first, check the resources, mounts, and node selection of the generated YAML, and then submit it following the normal deployment process.
After startup, confirm that the ports in the actual configuration of the three instances match the plan, and that the Coordinator `/readiness` returns `ready=true`.

Functional acceptance should check all of the following:

- Both Ps actually process requests, and D returns correct content; cover normal requests, streaming requests, and requests where both Ps work at the same time.

- The external Store write and read-back counts for repeated long input increase, and correspond to the request logs of this round.

- Transfer failures, timeouts, and internal retry logs are not masked by HTTP 200.

- The Pods do not restart unexpectedly, and the results are reproducible after redeployment.

To distinguish the local prefix cache from the external Store, you can use round-robin scheduling in the functional verification configuration and temporarily disable the engine's local prefix caching.
These settings are used to check the two Ps and the real external read-back; they are not uniform requirements for production scheduling or performance configuration.
