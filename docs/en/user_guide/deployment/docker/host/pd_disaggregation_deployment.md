# Docker-based Multi-Container PD-Disaggregated Deployment

This document guides users in deploying the PD disaggregation service based on Docker components. The user **executes a single command on the host to create a container and deploy the service**, but the container lifecycle must be managed manually by the user.

## Preparing the Image

Obtain the image in either of the following ways:

- Method 1: Downloading the official complete MindIE Motor image

  Go to the [Ascend official image repository](https://www.hiascend.com/en-sg/developer/ascendhub), enter "motor" in the search box, click the "mindie-motor" card, and on the "Image Version" tab, download the corresponding MindIE Motor image according to the device model.

- Method 2: Installing MindIE Motor in an existing image

  The base image already has components such as CANN, vLLM, and vLLM Ascend installed. Refer to [Installing MindIE Motor Based on an Existing Image](../../../maintenance/build_motor_image_from_vllm_ascend.md) to additionally install MindIE Motor.

After obtaining the image, use the following command to **load the image to each server**:

   ```bash
   docker load -i xxxx.tar
   ```

## Preparing the Service Startup Script

On **any server**, perform the following operations.

1. Obtain the startup script.

   Run the following commands to copy the `examples` directory from inside the image to the host (using the `/home/examples` path as an example):

   ```bash
   # Enter the image name loaded in the previous step
   IMAGE="<image name or image ID>"
   # Create a temporary container and record the container ID
   cid=$(docker create "$IMAGE")
   # Copy the examples folder to the host
   docker cp "$cid:/tmp/motor/examples" /home/examples
   # Delete the temporary container
   docker rm "$cid"
   ```

2. Prepare the configuration file.

     MindIE Motor has provided [PD disaggregation configuration examples](../../../../../../model_configs/vllm/models/README.md) for common models (deepseek_v4_flash, deepseek_v4_pro, GLM 5.1, etc.). Select a configuration example based on the model and server type, and **copy the configuration file to the /home/examples/config directory**.
     >[!NOTE]NOTE
     >The preceding Motor configuration provides only one feasible model splitting example. **Users can adjust the model partitioning strategy based on actual conditions** by focusing on the parameters in the following table, but the following formula must be satisfied:
     >
     > Number of NPU cards occupied by one P instance = single_p_instance_pod_num (number of Pods occupied, across how many machines) × p_pod_npu_num (number of NPUs occupied by each Pod) = data_parallel_size × tensor_parallel_size

      | Configuration Item | Value Type | Value Range | Configuration Description |
      | --- | --- | --- | --- |
      | p_instances_num | int | ≥1 | Number of Prefill instances. |
      | d_instances_num | int | ≥1| Number of Decode instances. |
      | single_p_instance_pod_num | int | ≥1 | Number of Pods into which one P instance is split.|
      | single_d_instance_pod_num | int | ≥1 | Number of Pods into which one D instance is split. |
      | p_pod_npu_num | int | ≥ 1, up to 16 cards per Pod | Number of NPU cards used by each P Pod. |
      | d_pod_npu_num | int | ≥ 1, up to 16 cards per Pod | Number of NPU cards used by each D Pod. |
      | data_parallel_size | int | ≥1 | Data parallel (DP) size. |
      | tensor_parallel_size | int | ≥1 | Tensor parallel (TP) size. |

3. <a id="step0003"></a>Synchronize the startup script configuration.

    Copy the entire `/home/examples` directory **to the same path on all servers**.

## Starting Service

1. Obtain the service startup command.

    Run the following command:

    ```bash
    python3 /home/examples/deployer/docker_deploy.py --config_dir /home/examples/config --show-commands
    ```

    The echo output is as follows (one inference container deployed per server). The following example has 2 Prefill and 2 Decode instances, with 1 container and 2 cards per instance:

    ```text
    Four servers are expected to be used.

    Server 1: management plane
    python3 /home/examples/deployer/docker_deploy.py \
      --config_dir /home/examples/config \
      --container-name motor-ctrl \
      --role coordinator,controller \
      --pod-ip "<local IP>" \
      --nic-name "<local NIC>"

    Server 1: Prefill p0
    python3 /home/examples/deployer/docker_deploy.py \
      --config_dir /home/examples/config \
      --container-name motor-p0 \
      --role prefill \
      --instance-name p0 \
      --pod-ip "<local IP>" \
      --nic-name "<local NIC>" \
      --coordinator-ip "<server 1 IP>" \
      --controller-ip "<server 1 IP>"

    Server 2: Prefill p1
    ...

    Server 3: Decode d0
    ...

    Server 4: Decode d1
    ...
    ```

2. Execute the service startup command.

    **Modify the preceding startup command** according to the following rules, and **execute the service startup command on the host of each server**.

    | Option | Description |
    | :--- | :--- |
    | --pod-ip | IP address of the current server.<br>You can run the `ip -4 route get 1.1.1.1` command to view it. The value after the `src` field is the `--pod-ip` value. |
    | --nic-name | Name of the primary NIC corresponding to the current server.<br>You can run the `ip -4 route get 1.1.1.1` command to view it. The value after the `dev` field is the `--nic-name` value. |
    | --coordinator-ip/--controller-ip | IP address of server 1. The management plane is deployed on server 1. |
    | --kv-store-ip | IP address of server 1. When pooling is enabled, kv_store is also deployed on server 1, on the same machine as the management plane. |

    [Optional] If you need to **deploy multiple inference containers on one server**, see [Deploying Multiple Inference Containers on the Same Server](#deploying-multiple-inference-containers-on-the-same-server) to deploy the service.

---

## (Optional) Enabling KV Pooling

If pooling is not enabled, you can skip this section.

After enabling, you also need to separately deploy a kv_store container. After executing `--show-commands` in [Starting Service](#starting-service), the echo output adds a kv_store command with the container name `motor-kvs` on server 1, and the management plane and Prefill/Decode commands add `--kv-store-ip`. For the pooling configuration of user_config.json, see [KV Pooling Capability](../../../features/kv_cache_store/README.md).

## Inference Verification

Send the following inference request on the **server where the deployment management service is deployed** to perform inference verification:

```bash
curl -X POST http://<IP address of the server where the coordinator management service resides>:1025/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "<model name, must be consistent with the configuration in the user_config.json file>",
    "messages": [{"role": "user", "content": "who are you?"}],
    "max_tokens": 36,
    "stream": true
  }'
```

If the following content is returned, the service is not ready yet. Retry later.

```Output
{"detail":"Service is not available"}
```

## Terminating and Redeploying the Service

Pressing `Ctrl+C` in the terminal terminates the service but does not exit the container. After the service is terminated, the following prompt is displayed to facilitate redeployment.

```text
The service in the container has been terminated. You can run the following command to redeploy the service.
python3 /path/to/examples/deployer/docker_deploy.py --config_dir /path/to/model_configs/vllm --start --container-name motor-ctrl --role coordinator,controller --pod-ip <local IP address> --nic-name <local primary NIC name>
Run log: examples/motor_workspace/motor-ctrl/ctrl/log/docker-coordinator-controller-<timestamp>.log
```

## (Optional) Enabling the Render Feature

Add the following configuration to `/home/examples/config/user_config.json`:

```json
{
  ...
  "motor_coordinator_config": {
    "render_config": {
      "enable": true
    }
  },
  ...
}
```

When deploying the management plane service by following the [service startup guide](#starting-service), an additional container named `<container-name>-vllm-render` is created, and vLLM Render is started inside it. When the management plane service is terminated, the render service is automatically terminated; when the management plane service is restarted, the render service is automatically restarted. After Render exits, the management plane service continues to run.

If `<container-name>` or `<container-name>-vllm-render` already exists, terminal deployment asks whether to delete it and continue. This step occurs before the port check: if the previous container has not been cleaned up, the ports it occupies are released together with the container and are no longer treated as port conflicts. Answering `y` deletes both the management plane container and the Render container, and then recreates them. Answering `N`, or executing in a non-interactive environment, aborts without overwriting.

## Tuning Guide

### Adjusting Container Mounts

The deployment script automatically creates containers for the user, and the containers automatically mount the paths of the user's service startup script and weight files. To customize the container creation command, you can manually adjust the docker-run template in the `examples/deployer/lib/constant.py` file.

### Deploying Multiple Inference Containers on the Same Server

Record the echo output of `--show-commands` in [Starting Service](#starting-service), and modify it as follows. The following example deploys 2 Prefill and 2 Decode instances on one 8-card server. Each instance occupies 1 container and 2 cards, with card numbers `0,1`, `2,3`, `4,5`, and `6,7` in sequence.

1. Management plane startup command

    No modification is required. Execute it according to the echo output.

    ```bash
    python3 /home/examples/deployer/docker_deploy.py \
      --config_dir /home/examples/config \
      --container-name motor-ctrl \
      --role coordinator,controller \
      --pod-ip "<local IP>" \
      --nic-name "<local NIC>"
    ```

2. kv_store startup command

    When pooling is not enabled, this command does not appear in the echo output. After pooling is enabled, no modification is required. Execute it according to the echo output.

    ```bash
    python3 /home/examples/deployer/docker_deploy.py \
      --config_dir /home/examples/config \
      --container-name motor-kvs \
      --role kv_store \
      --pod-ip "<local IP>" \
      --nic-name "<local NIC>" \
      --coordinator-ip "<local IP>" \
      --controller-ip "<local IP>"
    ```

3. Prefill inference container startup command

    Add the following five parameters.

    `motor-p0`:

    ```bash
    python3 /home/examples/deployer/docker_deploy.py \
      --config_dir /home/examples/config \
      --container-name motor-p0 \
      --role prefill \
      --instance-name p0 \
      --pod-ip "<local IP>" \
      --nic-name "<local NIC>" \
      --coordinator-ip "<server 1 IP>" \
      --controller-ip "<server 1 IP>" \
      --devices 0,1  \                  # Device IDs used by this container, 2 cards. They must not overlap on the same server
      --node-manager-port 4026  \       # NodeManager port. Stagger within the same server, for example, 4026, 4126
      --data-parallel-rpc-port 9000  \  # Data parallel RPC port. Stagger within the same server, for example, 9000, 9100
      --base-port 10000  \              # Engine endpoint start port, which also occupies a subsequent range of ports. Stagger within the same server, for example, 10000, 11000
      --kv-port 21000                   # KV transfer start port, which also occupies a subsequent range of ports. Stagger within the same server, for example, 21000, 22000
    ```

    `motor-p1`:

    ```bash
    python3 /home/examples/deployer/docker_deploy.py \
      --config_dir /home/examples/config \
      --container-name motor-p1 \
      --role prefill \
      --instance-name p1 \
      --pod-ip "<local IP>" \
      --nic-name "<local NIC>" \
      --coordinator-ip "<server 1 IP>" \
      --controller-ip "<server 1 IP>" \
      --devices 2,3  \                  # Device IDs used by this container, 2 cards. Must not overlap on the same server
      --node-manager-port 4126  \       # NodeManager port. Stagger within the same server, for example, 4026, 4126
      --data-parallel-rpc-port 9100  \  # Data parallel RPC port. Stagger within the same server, for example, 9000, 9100
      --base-port 11000  \              # Engine endpoint start port, which additionally occupies a subsequent range of ports. Stagger within the same server, for example, 10000, 11000
      --kv-port 22000                   # KV transfer start port, which additionally occupies a subsequent range of ports. Stagger within the same server, for example, 21000, 22000
    ```

4. Decode inference container startup command

    Add the following five parameters.

    `motor-d0`:

    ```bash
    python3 /home/examples/deployer/docker_deploy.py \
      --config_dir /home/examples/config \
      --container-name motor-d0 \
      --role decode \
      --instance-name d0 \
      --pod-ip "<local IP>" \
      --nic-name "<local NIC>" \
      --coordinator-ip "<server 1 IP>" \
      --controller-ip "<server 1 IP>" \
      --devices 4,5  \                  # Device IDs used by this container, 2 cards. They must not overlap on the same server
      --node-manager-port 4226  \       # NodeManager port. Stagger within the same server, for example, 4226 and 4326
      --data-parallel-rpc-port 9200  \  # Data parallel RPC port. Staggered  on the same server, for example, 9200 and 9300
      --base-port 12000  \              # Engine endpoint start port, which also occupies a subsequent range of ports. Stagger within the same server, for example, 12000 and 13000
      --kv-port 23000                   # KV transfer start port, which also occupies a subsequent range of ports. Stagger within the same server, for example, 23000 and 24000
    ```

    `motor-d1`:

    ```bash
    python3 /home/examples/deployer/docker_deploy.py \
      --config_dir /home/examples/config \
      --container-name motor-d1 \
      --role decode \
      --instance-name d1 \
      --pod-ip "<local IP>" \
      --nic-name "<local NIC>" \
      --coordinator-ip "<server 1 IP>" \
      --controller-ip "<server 1 IP>" \
      --devices 6,7  \                  # Device IDs used by this container, 2 cards. Must not overlap on the same server
      --node-manager-port 4326  \       # NodeManager port. Stagger within the same server, for example, 4226 and 4326
      --data-parallel-rpc-port 9300  \  # Data parallel RPC port. Stagger within the same server, for example, 9200 and 9300
      --base-port 13000  \              # Engine endpoint start port, which also occupies a subsequent range of ports. Stagger within the same server, for example, 12000 and 13000
      --kv-port 24000                   # KV transfer start port, which also occupies a subsequent range of ports. Stagger within the same server, for example, 23000 and 24000
    ```
