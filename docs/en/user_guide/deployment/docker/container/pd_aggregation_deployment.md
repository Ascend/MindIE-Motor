# Docker-based In-Container PD-Aggregated Deployment

This document guides users in deploying the PD aggregation service inside an already-created container. The container is created by a cloud platform (for example, ModelArts) or by the user. After entering the container, the user executes a single command to start the service.

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
       "hybrid_instances_num": 2,
       "single_hybrid_instance_pod_num": 1,
       "hybrid_pod_npu_num": 2
     }
   }
   ```

   >[!NOTE]Note
   >The preceding Motor configuration provides only one feasible model sharding example. **Users can adjust the model partitioning strategy according to actual conditions** by focusing on the parameters in the following table, but the following formula must be satisfied:
   >
   > Number of NPU cards occupied by one aggregation instance = `single_hybrid_instance_pod_num` (number of Pods occupied, across how many machines) × `hybrid_pod_npu_num` (number of NPUs occupied by each Pod) = `data_parallel_size` × `tensor_parallel_size`

   | Configuration Item | Value Type | Value Range | Configuration Description |
   | --- | --- | --- | --- |
   | hybrid_instances_num | int | ≥ 1 | Number of aggregation instances. |
   | single_hybrid_instance_pod_num | int | ≥ 1 | Number of Pods into which one aggregation instance is split. |
   | hybrid_pod_npu_num | int | ≥ 1, up to 16 cards per Pod | Number of NPU cards used by each aggregation Pod. |
   | data_parallel_size | int | ≥ 1 | Data parallel (DP) size |
   | tensor_parallel_size | int | ≥ 1 | Tensor parallel (TP) size |

3. <a id="step0003"></a>Synchronize the startup script configuration.

   Copy the entire `/home/examples` directory **to the same path on all servers**.

## Creating a Container

Create containers on the servers in the cluster according to the plan. A total of three types of containers need to be created. The container names are consistent with `--container-name` in the output of [Starting Service](#starting-service). The templates in `examples/deployer/lib/constant.py` can be used for reference.

| Container Type | Reference Template | Configuration Description |
| --- | --- | --- |
| Control plane | [ENTER_DOCKER_RUN_CTRL](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.2.0/examples/deployer/lib/constant.py#L607-L618) | Container name `motor-ctrl`, created on server 1. |
| Aggregation | Atlas 800I A2: [ENTER_DOCKER_RUN_A2](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.2.0/examples/deployer/lib/constant.py#L444-L476)<br>Atlas 800I A3: [ENTER_DOCKER_RUN_A3](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.2.0/examples/deployer/lib/constant.py#L478-L518)<br>Ascend 950 (8 cards): [ENTER_DOCKER_RUN_A5](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.2.0/examples/deployer/lib/constant.py#L520-L557)<br>Ascend 950 (16 cards): [ENTER_DOCKER_RUN_A5_POD16](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.2.0/examples/deployer/lib/constant.py#L559-L605) | When one instance occupies only one container, the container names are `motor-u0` and `motor-u1`. When one instance is split into multiple containers, the container names are `motor-u0-0` and `motor-u0-1`. Only one container is created on each server. |
| kv_store | [ENTER_DOCKER_RUN_KVS](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.2.0/examples/deployer/lib/constant.py#L620-L634) | Created when pooling is enabled, with the container name `motor-kvs`, placed on server 1. |

When creating a container, you must mount the service startup script folder into the container (for example, `-v /home/examples:/home/examples`). After creation, enter the container:

```bash
docker exec -it <container name> bash
```

## Starting Service

By default, one server deploys only one aggregation inference container. To **deploy multiple aggregation inference instances on one server (for small models only)**, see [Deploying Multiple Inference Instances in the Same Container](#deploying-multiple-inference-instances-in-the-same-container).

1. Obtain the service startup command

   Execute this on the server where `/home/examples` has been prepared. This command only prints the startup command.

   ```bash
   python3 /home/examples/deployer/docker_deploy.py --config_dir /home/examples/config --show-commands --start
   ```

   The output is as follows:

   ```text
   Two servers are expected to be used.

   Server 1: control plane
   python3 /home/examples/deployer/docker_deploy.py \
     --config_dir /home/examples/config \
     --start \
     --container-name motor-ctrl \
     --role coordinator,controller \
     --pod-ip "<local IP>" \
     --nic-name "<local NIC>"

   Server 1: aggregation u0
   python3 /home/examples/deployer/docker_deploy.py \
     --config_dir /home/examples/config \
     --start \
     --container-name motor-u0 \
     --role union \
     --instance-name u0 \
     --pod-ip "<local IP>" \
     --nic-name "<local NIC>" \
     --coordinator-ip "<server 1 IP>" \
     --controller-ip "<server 1 IP>"

   Server 2: aggregation u1
   python3 /home/examples/deployer/docker_deploy.py \
     --config_dir /home/examples/config \
     --start \
     --container-name motor-u1 \
     --role union \
     --instance-name u1 \
     --pod-ip "<local IP>" \
     --nic-name "<local NIC>" \
     --coordinator-ip "<server 1 IP>" \
     --controller-ip "<server 1 IP>"
   ```

   After pooling is enabled, the output adds a kv_store command with the container name `motor-kvs` on server 1, and the control plane and aggregation commands add `--kv-store-ip`.

2. Execute the service startup command.

   Enter the corresponding container in the echo output, **modify the preceding startup command** according to the following rules, and then execute it. Execute it only once in each aggregation container.

   | Option | Description |
   | :--- | :--- |
   | --start | Starts the service in the current container. |
   | --container-name | Name of the current container, which must be consistent with the name used when creating the container. |
   | --pod-ip | IP address of the current server.<br>You can run the `ip -4 route get 1.1.1.1` command to view it. The value after the `src` field is the `--pod-ip` value. |
   | --nic-name | Name of the primary NIC corresponding to the current server.<br>You can run the `ip -4 route get 1.1.1.1` command to view it. The value after the `dev` field is the `--nic-name` value. |
   | --coordinator-ip/--controller-ip | IP address of server 1. The control plane is deployed on server 1. |
   | --kv-store-ip | IP address of server 1. When pooling is enabled, kv_store is also deployed on server 1, on the same machine as the control plane. |

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

After enabling pooling, send another request with a prompt of no fewer than 128 tokens. After the request succeeds, check `Keys` in the kv_store log. A change from 0 to a value greater than 0 indicates that the blocks have been pooled. Example log path:

```text
/home/examples/motor_workspace/motor-kvs/kvs/log/docker-kv_store-<timestamp>.log
```

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

On the server where the control plane is deployed, additionally create a container named `motor-ctrl-vllm-render`. This container does not mount NPUs. For the mount configuration, refer to the control plane template [ENTER_DOCKER_RUN_CTRL](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.2.0/examples/deployer/lib/constant.py#L607-L618). Add `-v /var/run/docker.sock:/var/run/docker.sock` to the control plane container.

After entering the Render container, execute:

```bash
python3 /home/examples/deployer/docker_deploy.py --config_dir /home/examples/config --start --role render
```

When `--start` is executed in the control plane container, the Render container is restarted. When the control plane process exits, the Render container is stopped. After Render exits, the Coordinator continues to run.

## Tuning Guide

### Adjusting Container Mounts

The template for creating a container is for reference only. When adjusting mounts, add or remove `-v` in the `docker run` command. When creating a container on a cloud platform, adjust the mount configuration on the platform. The host path must actually exist.

### Deploying Multiple Inference Instances in the Same Container

You can execute `--start` multiple times in one container to deploy multiple inference instances.

The following example deploys 2 aggregation instances in 1 container, with the container mounting cards `0,1,2,3`. Aggregation `u0` uses `0,1`, and aggregation `u1` uses `2,3`.

1. Create a container.

   Execute once on the host. This container mounts the cards to be used by the two instances. The control plane still creates a separate container, which is executed according to the output of [Starting Service](#starting-service).

   ```bash
   python3 /home/examples/deployer/docker_deploy.py \
     --config_dir /home/examples/config \
     --create \
     --container-name motor-hybrid \
     --devices 0,1,2,3    # This container mounts the cards to be used by the two instances
   ```

2. Enter the container.

   ```bash
   docker exec -it motor-hybrid bash
   ```

3. Execute the aggregation u0 startup command.

   Add the following five parameters additionally.

   ```bash
   python3 /home/examples/deployer/docker_deploy.py \
     --config_dir /home/examples/config \
     --start \
     --container-name motor-hybrid \
     --role union \
     --instance-name u0 \
     --pod-ip "<local IP>" \
     --nic-name "<local NIC>" \
     --coordinator-ip "<server 1 IP>" \
     --controller-ip "<server 1 IP>" \
     --devices 0,1  \                  # Card numbers used by this instance, 2 cards. Other instances in the same container must not overlap
     --node-manager-port 4026  \       # NodeManager port. Stagger within the same container, for example, 4026 and 4226
     --data-parallel-rpc-port 9000  \  # Data parallel RPC port. Stagger within the same container, for example, 9000 and 9200
     --base-port 10000  \              # Engine endpoint starting port, which will occupy a subsequent range of ports. Stagger within the same container, for example, 10000 and 12000
     --kv-port 21000                   # KV transfer starting port, which will occupy a subsequent range of ports. Stagger within the same container, for example, 21000 and 23000
   ```

4. Execute the aggregation u1 startup command.

   Open a new window, enter the container, and execute the following command, adding the following five parameters.

   ```bash
   python3 /home/examples/deployer/docker_deploy.py \
     --config_dir /home/examples/config \
     --start \
     --container-name motor-hybrid \
     --role union \
     --instance-name u1 \
     --pod-ip "<local IP>" \
     --nic-name "<local NIC>" \
     --coordinator-ip "<server 1 IP>" \
     --controller-ip "<server 1 IP>" \
     --devices 2,3  \                  # Card numbers used by this instance, 2 cards. Other instances in the same container must not overlap
     --node-manager-port 4226  \       # NodeManager port. Stagger within the same container, for example, 4026 and 4226
     --data-parallel-rpc-port 9200  \  # Data parallel RPC port. Stagger within the same container, for example, 9000 and 9200
     --base-port 12000  \              # Engine endpoint starting port, which will occupy a subsequent range of ports. Stagger within the same container, for example, 10000 and 12000
     --kv-port 23000                   # KV transfer starting port, which will occupy a subsequent range of ports. Stagger within the same container, for example, 21000 and 23000
   ```
