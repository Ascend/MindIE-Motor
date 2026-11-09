# Docker-based Multi-Container PD-Aggregated Deployment

This document guides users in deploying the PD aggregated deployment service based on Docker components. The user **executes a single command on the host to create a container and deploy the service**, but the container lifecycle must be managed manually by the user.

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

   MindIE Motor provides a [PD aggregation configuration example](../../../../../../model_configs/vllm/pd_hybrid/README.md). The number of instances is specified by `hybrid_instances_num`, the number of containers for a single instance by `single_hybrid_instance_pod_num`, the number of cards per container by `hybrid_pod_npu_num`, the engine configuration in `motor_engine_union_config`, and the environment variables in `motor_engine_union_env`. **Copy the configuration files to the `/home/examples/config` directory**.

   ```json
   {
     "motor_deploy_config": {
       "hybrid_instances_num": 4,
       "single_hybrid_instance_pod_num": 1,
       "hybrid_pod_npu_num": 2
     }
   }
   ```

   >[!NOTE] Note
   >The preceding Motor configuration provides only one feasible model sharding example. Users can adjust the model partitioning strategy according to actual conditions by focusing on the parameters in the following table, but the following formula must be satisfied:
   >
   >Number of NPU cards occupied by one aggregation instance = `single_hybrid_instance_pod_num` (number of Pods occupied, across how many machines) × `hybrid_pod_npu_num` (number of NPUs occupied by each Pod) = `data_parallel_size` × `tensor_parallel_size`

   | Configuration Item | Value Type | Value Range | Configuration Description |
   | :--- | :--- | :--- | :--- |
   | hybrid_instances_num | int | ≥1 | Number of aggregation instances. |
   | single_hybrid_instance_pod_num | int | ≥1 | Number of Pods into which one aggregation instance is split. |
   | hybrid_pod_npu_num | int | ≥ 1, up to 16 cards per Pod | Number of NPU cards used by each aggregation Pod. |
   | data_parallel_size | int | ≥1 | Data parallel (DP) size. Written in `motor_engine_union_config.engine_config`. |
   | tensor_parallel_size | int | ≥1 | Tensor parallel (TP) size. Written in `motor_engine_union_config.engine_config`. |

3. <a id="step0003"></a>Synchronize the startup script configuration.

   Copy the entire `/home/examples` directory **to the same path on all servers**.

## Starting Service

1. Obtain the service startup command

   Run the following command:

   ```bash
   python3 /home/examples/deployer/docker_deploy.py --config_dir /home/examples/config --show-commands
   ```

   The output is as follows (one server deploys one inference container). The following example has 4 aggregation instances, with 1 container and 2 cards per instance:

   ```text
   Four servers are expected to be used.

   Server 1: management plane
   python3 /home/examples/deployer/docker_deploy.py \
     --config_dir /home/examples/config \
     --container-name motor-ctrl \
     --role coordinator,controller \
     --pod-ip "<local IP>" \
     --nic-name "<local NIC>"

   Server 1: aggregation u0
   python3 /home/examples/deployer/docker_deploy.py \
     --config_dir /home/examples/config \
     --container-name motor-u0 \
     --role union \
     --instance-name u0 \
     --pod-ip "<local IP>" \
     --nic-name "<local NIC>" \
     --coordinator-ip "<server 1 IP>" \
     --controller-ip "<server 1 IP>"

   Server 2: aggregation u1
   ...

   Server 3: aggregation u2
   ...

   Server 4: aggregation u3
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

## Inference Verification

Send the following inference request on the **server where the deployment management service is deployed** to perform inference verification:

```bash
curl -X POST http://<IP address of the server where the coordinator management service is located>:1025/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "<model name, which must be consistent with the configuration in the user_config.json file>",
    "messages": [{"role": "user", "content": "who are you?"}],
    "max_tokens": 36,
    "stream": true
  }'
```

If the following content is returned, the service is not ready yet. Retry later.

```Output
{"detail":"Service is not available"}
```

---

## Terminating and Redeploying the Service

Pressing `Ctrl+C` in the terminal terminates the service but does not exit the container. After the service is terminated, the following prompt is displayed to facilitate redeployment.

```text
The service in the container has been terminated. You can run the following command to redeploy the service.
python3 /path/to/examples/deployer/docker_deploy.py --config_dir /path/to/model_configs/vllm --start --container-name motor-u0 --role union --instance-name u0 --pod-ip <local IP address> --nic-name <local primary NIC name>
Running log: examples/motor_workspace/motor-u0/u0/log/docker-union-u0-<timestamp>.log
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

Record the echo output of `--show-commands` in [Starting Service](#starting-service), and modify it as follows. The following example deploys 4 aggregation instances on one 8-card server. Each instance occupies 1 container and 2 cards, with card numbers `0,1`, `2,3`, `4,5`, and `6,7` in sequence.

1. Control plane startup command

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

3. Aggregation deployment inference container startup command

   Add the following five parameters.

   `motor-u0`:

   ```bash
   python3 /home/examples/deployer/docker_deploy.py \
     --config_dir /home/examples/config \
     --container-name motor-u0 \
     --role union \
     --instance-name u0 \
     --pod-ip "<local IP>" \
     --nic-name "<local NIC>" \
     --coordinator-ip "<server 1 IP>" \
     --controller-ip "<server 1 IP>" \
     --devices 0,1  \                  # Card numbers used by this container, 2 cards. Must not overlap on the same server
     --node-manager-port 4026  \       # NodeManager port. Stagger on the same server, for example, 4026 and 4126
     --data-parallel-rpc-port 9000  \  # Data parallel RPC port. Stagger within the same server, for example, 9000 and 9100
     --base-port 10000  \              # Engine endpoint starting port, which will occupy a subsequent range of ports. Stagger within the same server, for example, 10000 and 11000
     --kv-port 21000                   # KV transfer starting port, which will occupy a subsequent range of ports. Stagger within the same server, for example, 21000 and 22000
   ```

   `motor-u1`:

   ```bash
   python3 /home/examples/deployer/docker_deploy.py \
     --config_dir /home/examples/config \
     --container-name motor-u1 \
     --role union \
     --instance-name u1 \
     --pod-ip "<local IP>" \
     --nic-name "<local NIC>" \
     --coordinator-ip "<server 1 IP>" \
     --controller-ip "<server 1 IP>" \
     --devices 2,3  \                  # Card numbers used by this container, 2 cards. Must not overlap on the same server
     --node-manager-port 4126  \       # NodeManager port. Stagger within the same server, for example, 4026 and 4126
     --data-parallel-rpc-port 9100  \  # Data parallel RPC port. Stagger within the same server, for example, 9000 and 9100
     --base-port 11000  \              # Engine endpoint starting port, which will occupy a subsequent range of ports. Stagger within the same server, for example, 10000 and 11000
     --kv-port 22000                   # KV transfer starting port, which will occupy a subsequent range of ports. Stagger within the same server, for example, 21000 and 22000
   ```

   `motor-u2`:

   ```bash
   python3 /home/examples/deployer/docker_deploy.py \
     --config_dir /home/examples/config \
     --container-name motor-u2 \
     --role union \
     --instance-name u2 \
     --pod-ip "<local IP>" \
     --nic-name "<local NIC>" \
     --coordinator-ip "<server 1 IP>" \
     --controller-ip "<server 1 IP>" \
     --devices 4,5  \                  # Card numbers used by this container, 2 cards. Must not overlap on the same server
     --node-manager-port 4226  \       # NodeManager port. Stagger within the same server, for example, 4226 and 4326
     --data-parallel-rpc-port 9200  \  # Data parallel RPC port. Stagger within the same server, for example, 9200 and 9300
     --base-port 12000  \              # Engine endpoint starting port, which will occupy a subsequent range of ports. Stagger within the same server, for example, 12000 and 13000
     --kv-port 23000                   # KV transfer starting port, which will occupy a subsequent range of ports. Stagger within the same server, for example, 23000 and 24000
   ```

   `motor-u3`:

   ```bash
   python3 /home/examples/deployer/docker_deploy.py \
     --config_dir /home/examples/config \
     --container-name motor-u3 \
     --role union \
     --instance-name u3 \
     --pod-ip "<local IP>" \
     --nic-name "<local NIC>" \
     --coordinator-ip "<server 1 IP>" \
     --controller-ip "<server 1 IP>" \
     --devices 6,7  \                  # Card numbers used by this container, 2 cards. Must not overlap on the same server
     --node-manager-port 4326  \       # NodeManager port. Stagger on the same server, for example, 4226 and 4326
     --data-parallel-rpc-port 9300  \  # Data parallel RPC port. Stagger on the same server, for example, 9200 and 9300
     --base-port 13000  \              # Engine endpoint starting port, which will occupy a subsequent range of ports. Stagger on the same server, for example, 12000 and 13000
     --kv-port 24000                   # KV transfer starting port, which will occupy a subsequent range of ports. Stagger on the same server, for example, 23000 and 24000
   ```
