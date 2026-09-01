# Mooncake Backend

The Mooncake pooled backend is natively integrated by vllm-ascend, and **no additional components need to be installed**.

## Configuration

Configure `"backend": "mooncake"` in `AscendStoreConnector`:

```json
"backend": "mooncake"
```

Configure `"backend": "mooncake"` in `kv_cache_store_config`:

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

`eviction_high_watermark_ratio` and `eviction_ratio` are Mooncake-specific parameters and are passed to the `mooncake_master` process.

## Environment Variable Configuration Description

Configure the following environment variables in `motor_engine_prefill_env` and `motor_engine_decode_env` of `env.json` according to the hardware. Keep Prefill and Decode consistent.

| Hardware | Dependency | Environment Variable (Written to `env.json`) | Description |
|------|------|------------------------------|------|
| Ascend 950 series products | HDK >= 25.6 and mooncake >= v0.3.11<br>CANN >= 9.1.0 | **UBOE**: `ASCEND_GLOBAL_RESOURCE_CONFIG={"comm_resource_config.protocol_desc":["uboe:device"]}`<br>**UB**: `ASCEND_LOCAL_COMM_RES={"version":"1.3"}` | Configure the corresponding environment variable according to the communication protocol actually used (choose either UBOE or UB). |
| Atlas 800I/T A3 SuperPoD Server | HDK >= 26.0<br>or HDK >= 25.5 and mooncake >= v0.3.11<br>CANN >= 9.0.0<br>Lingqu computing network >= 1.5 | `ASCEND_ENABLE_USE_FABRIC_MEM=1` | **Recommended**. Enable the unified memory address direct transfer solution. If SSD offload is enabled, the related memory size must be aligned to 1 GB. For details, see the vllm-ascend document [Fabric memory size alignment](https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/kv_pool.html#fabric-memory-size-alignment-a3-ascend-enable-use-fabric-mem-1). |
| Atlas 800I/T A3 SuperPoD Server | When the above dependencies are not met | `ASCEND_BUFFER_POOL=4:8` | Configure the number and size of buffers on the NPU device used for aggregation and KV transfer (for example, `4:8` indicates four 8 MB buffers). |
| Atlas 800I/T A2 inference server | HDK >= 25.5 recommended | `HCCL_INTRA_ROCE_ENABLE=1` | Required by the direct transfer solution of the 800 I/T A2 series. |

> For more principles and troubleshooting, see the [vllm-ascend KV Pool document](https://docs.vllm.ai/projects/ascend/en/main/user_guide/feature_guide/kv_pool.html).

## Tuning Suggestions

- **`global_segment_size`**: Adjust it based on the model size and concurrency. A value that is too small causes frequent eviction, while a value that is too large wastes video memory. It is recommended to set it to 1.5 to 2 times the estimated KV Cache size of the model.

- **`eviction_high_watermark_ratio`** and **`eviction_ratio`**: Eviction is triggered when the pooled space usage reaches `eviction_high_watermark_ratio`, and each eviction reclaims space at the `eviction_ratio` ratio. In high-concurrency scenarios, you can moderately reduce the eviction ratio to reduce jitter.

- **`default_kv_lease_ttl`**: Control the lease validity period of KV objects. Ensure that it is greater than the transfer timeout (`ASCEND_CONNECT_TIMEOUT`/`ASCEND_TRANSFER_TIMEOUT`) to prevent the lease from expiring before the transfer is complete.

## Additional Configuration for Ascend 950 Series Products

When using the Mooncake backend for KV pooling on the Atlas 850 SuperPoD Server, you must currently ensure that the Pod can access the UB-related NICs (`ipourma*`) on the host side. Choose either of the following methods.

### Method 1: Using the Host Network

1. **Configuring the host network for Prefill/Decode**

   When deploying with the default CRD (`deploy_mode` is `infer_service_set`), edit `examples/deployer/yaml_template/infer_service_template.yaml`, and add the following fields under `spec.template.spec` in both the `- name: prefill` and `- name: decode` definitions in `roles` (both must be configured):

   ```yaml
   - name: prefill   # Modify the decode role in the same way
     # ...
     spec:
       template:
         spec:
           hostNetwork: true
           dnsPolicy: ClusterFirstWithHostNet
           schedulerName: volcano
           # ... Keep the remaining original fields unchanged
   ```

   > If `motor_deploy_config.deploy_mode` in `user_config.json` is `multi_deployment`, modify `examples/deployer/yaml_template/engine_template.yaml`: add the same fields as above (`hostNetwork: true` and `dnsPolicy: ClusterFirstWithHostNet`) under `spec.template.spec` of the engine Pod.

2. **Adding engine environment variables for the Atlas 850 SuperPoD Server**

   Edit `set_a5_engine_env` in `examples/deployer/startup/common.sh` (invoked by `roles/engine.sh` in the Atlas 850 SuperPoD Server scenario), and complete the implementation as follows:

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

   > The script above selects the first NIC of the default route from `/proc/net/route` as the `GLOO`/`TP`/`HCCL` communication NIC. Ensure that this NIC is the primary NIC of the server.

   After completing the modifications above, redeploy the service.

### Method 2: Mounting the Host ipourma NIC into the Pod

The following mount operations must be performed separately on **each server where inference instances are deployed**.

**Mounting into the Pod:**

1. **(Mandatory) Backing up IPv6**

   ```bash
   BACKUP=/tmp/ipourma_backup_latest.txt
   : > "$BACKUP"
   for i in $(seq 0 9); do
     echo "===== ipourma$i =====" | tee -a "$BACKUP"
     ip -6 addr show dev ipourma$i 2>/dev/null | tee -a "$BACKUP"
   done
   ```

2. **Locating the business container and querying its PID**

   Run `docker ps` on the local machine and distinguish the pause sandbox from the inference business container by container name. Under Docker, K8s container names are roughly as follows:

   ```text
   k8s_<container name>_<Pod name>_<namespace>_<PodUID>_<retry count>
   ```

   | Fragment | Description | Example |
   |------|------|------|
   | `k8s_POD_...` | pause sandbox, **do not use** | `k8s_POD_vllm-0-decode-0-0_mindie-motor_...` |
   | `k8s_vllm_...` | **business container** (the container name in the image is usually `vllm`) | `k8s_vllm_vllm-0-decode-0-0_mindie-motor_...` |

   The second row `k8s_vllm_...` in the table above is the business container. All subsequent operations, including obtaining the PID and mounting the NIC, should target this container.

   ```bash
   # Replace <Pod name keyword> with the actual value, for example, vllm-0-decode-0-0. Exclude the k8s_POD_ sandbox
   CNAME=$(docker ps --format '{{.Names}}' | grep '<Pod name keyword>' | grep -v 'k8s_POD_' | head -1)
   PID=$(docker inspect -f '{{.State.Pid}}' "$CNAME")
   echo "CNAME=$CNAME PID=$PID"
   ```

3. **Exposing the network namespace**

   Replace `<custom name>` with a memorable name (such as `decode` or `prefill`):

   ```bash
   NS_NAME=<custom name>
   mkdir -p /var/run/netns
   ln -sf /proc/$PID/ns/net /var/run/netns/$NS_NAME
   ip netns list
   ```

4. **Moving the NIC into the Pod**

   ```bash
   for i in $(seq 0 9); do
     ip link show dev ipourma$i >/dev/null 2>&1 && ip link set ipourma$i netns "$NS_NAME"
   done
   ```

5. **Enabling IPv6 in the Pod netns, starting the NIC, and restoring the address**

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

6. **Verification**

   ```bash
   ip netns exec "$NS_NAME" ip -br link | grep ipourma
   nsenter -t "$PID" -n python3 -c "import socket; print(socket.if_nametoindex('ipourma0'))"
   ```

   After the mount succeeds, NICs such as `ipourma0` to `ipourma9` should be visible in the container (the specific names and addresses depend on the actual environment).

**Moving back to the host (rollback)**

Execute this **before** deleting or recreating the Pod. `<name used at that time>` must be consistent with the `NS_NAME` used when mounting:

```bash
NS_NAME=<name used at that time>
BACKUP=/tmp/ipourma_backup_latest.txt

# 1. Move from the Pod netns back to the host
for i in $(seq 0 9); do
  ip netns exec "$NS_NAME" ip link show dev ipourma$i >/dev/null 2>&1 && \
    ip netns exec "$NS_NAME" ip link set ipourma$i netns 1
done

# 2. Restore IPv6 on the host
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

# 3. Delete the soft link
rm -f /var/run/netns/"$NS_NAME"

# 4. Confirm
ip -br link | grep ipourma
```

>[!NOTE]NOTE
>
>- An `ipourma` interface can belong to only one network namespace at a time. Multiple engine Pods on the same node must not contend for the same set of interfaces.
>- If the Pod is deleted without rollback, the NIC may be lost and can be recovered only by reloading the driver or restarting the node.
>- If `IPv6 is disabled on this device` is reported, run `sysctl -w net.ipv6.conf.<NIC>.disable_ipv6=0` on the interface first.
