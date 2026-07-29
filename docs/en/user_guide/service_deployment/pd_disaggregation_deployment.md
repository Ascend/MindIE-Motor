# Prefill-Decode Disaggregation

## Use Cases

### Introduction

Prefill-Decode (PD) Disaggregation runs the prefill and decode phases of LLM inference on separate instances. It is suitable for scenarios with high requirements for latency and throughput. PD disaggregation improves NPU utilization, reduces mutual interference caused by   of Prefill and Decode, and increases the overall throughput at the same latency.

The two inference phases are described as follows:

- **Prefill phase**: Performs a full forward pass on the input prompt to generate initial hidden states. This phase is compute-intensive and must be executed once per new input sequence.
- **Decode phase**: Gradually generates subsequent tokens based on the Prefill result. Each step computes only the activation and attention of the latest token, resulting in low per-step computation. However, this process must be repeated until generation concludes, making it memory-intensive—primarily dominated by accesses to KV Cache and other memory operations.

This repository uses a **multi-node PD disaggregation** deployment scheme. Kubernetes Services are used to expose the inference entry point for the Coordinator. Multiple Deployments are used to deploy the Controller (single pod), Coordinator (single pod), and Server (multiple pods for both P and D instances). The Controller manages clusters and instances. The Coordinator receives user requests and schedules them to P/D instances. The P and D instances collaborate to complete a complete inference.

**Advantages of PD disaggregation**

- **Better resource utilization**: Prefill is compute-intensive, while Decode is memory access-intensive. Given their distinct characteristics, separate deployment allows for more efficient use of NPU compute and bandwidth resources.
- **Higher throughput**: While Prefill processes new requests, Decode can continuously process the decoding of existing requests, resulting in a higher overall processing capability.
- **More controllable latency**: The separation of the two phases reduces queuing and waiting time, especially in high-concurrency scenarios, helping to reduce latency.

### Deployment Entry and Process

The deployment process focuses on three entries:

1. `user_config.json`: total configuration of deployment and services (number of instances, images, models, parallel policies, TLS, and Controller/Coordinator).
2. `env.json`: environment variables of each component (such as CANN, HCCL, and OMP), which are injected by `deploy.py` into `boot.sh`.
3. Deployment script `deploy.py`: reads the preceding configurations, generates Kubernetes YAML files, updates `boot.sh`, creates ConfigMaps, and executes `kubectl apply`.

**Deployment mode**: Currently, the **CRD mode** (MindCluster-based infer-operator) is used by default. The adaptation and verification of the reliability, availability, and serviceability (RAS) and pooling capability have not been completed in this mode. If you need the RAS or KV pooling capability, you can set `motor_deploy_config.deploy_mode` in `user_config.json` to `multi_deployment` to switch to the original **multi-YAML deployment mode** (multiple Deployment YAML files are generated and applied by `deploy.py`). This mode supports RAS and pooling capabilities.

### Restrictions

- The Atlas 800I A2 inference server and Atlas 800I A3 SuperPoD server support this feature.
- The devices running on both the prefill and decode nodes must be of the same type.
- NPU network ports are interconnected.
- The supported models are the same as those of vllm-ascend.

### Hardware Requirements

The following table lists the hardware environment supported by prefill-decode disaggregation deployment.

**Table 1** Supported hardware

| Type  | Model                      | Memory    |
|--------|----------------------------|----------|
| Server| Atlas 800I A2 inference server  | 32/64 GB|
| Server| Atlas 800I A3 SuperPoD server| 64 GB    |

>[!NOTE]NOTE
>
>- A cluster must support parameter plane interconnection. This means that the server NPU ports must be in the same VLAN and capable of communication through RoCE.
>- To maintain service stability, users should strictly control the permissions of custom pods to prevent high-privilege pods from modifying internal parameters of MindIE, which may cause exceptions.

## Preparing an Image

Before deploying the PD disaggregation service, prepare available inference images on each compute node. You are advised to use verified preset images. If you only obtain a base (bare) image, you need to create a new image and install vLLM, vllm-ascend, and MindIE-PyMotor in the image.

>[!NOTE]NOTE
>Regardless of whether a preset image or a custom image is used, all Kubernetes nodes involved in the deployment (including the worker nodes running the Controller, Coordinator, P instances, and D instances) must be able to locally load the image. Otherwise, the pod may be in the `ImagePullBackOff` or `ErrImagePull` state due to an unavailable image.

### Using Recommended Preset Images

You are advised to use official or inference images that have been verified in the production environment (obtain the images from [Image Address](https://www.hiascend.com/en/developer/ascendhub)). These images have the following pre-installed:

- vLLM and its Ascend-adapted components (such as vllm-ascend)
- MindIE-PyMotor and its basic dependencies.

If the production environment cannot directly access the image repository and the offline package (`.tar` file) needs to be imported, you can select the loading mode based on the container runtime type on each node. (The address for obtaining the `.tar` package is the same as the [address for obtaining the image](https://www.hiascend.com/en/developer/ascendhub).)

**Table 2** Image loading examples for different runtime environments

| Runtime Type| Example Command for Loading an Image|
|-----------|------------------|
| Docker    | `docker load -i mindie-motor-vllm-dev.tar` |
| containerd (using `ctr`)| `ctr -n k8s.io images import mindie-motor-vllm-dev.tar` |
| containerd (using `nerdctl`)| `nerdctl -n k8s.io load -i mindie-motor-vllm-dev.tar` |

> [!NOTE]NOTE
> After the import is complete, you can run the `docker images`, `ctr -n k8s.io images list`, or `nerdctl -n k8s.io images` command to check whether the image is successfully imported. The image name must be the same as that configured in `image_name`.

### Building a Custom Image Based on a Bare Image

If you have obtained only one base (bare) image (containing only the OS, CANN, and Python, without vLLM, vllm-ascend, or MindIE-PyMotor pre-installed), you need to install vLLM/vllm-ascend and this repository in the image, and then commit the container as an image for deployment. For details on base image selection, installation of vLLM and vllm-ascend, and version compatibility requirements, see the relevant sections below.

#### Installing and Compiling MindIE-PyMotor in the Container

Start a container on the base image where vLLM and vllm-ascend have been installed, place the source code of this repository (MindIE-PyMotor) into the container (for example, `/home/PyMotor`), and run the following commands in the root directory of the source code: First, use `requirements.txt` to install dependencies, then run `bash build.sh` to compile and generate a wheel package, and finally install this repository.

```bash
cd /home/PyMotor
pip install -r requirements.txt
bash build.sh
pip install dist/*.whl
```

#### Committing the Container as an Image

After the environment and MindIE-PyMotor are installed, commit the container as an image on the **host machine where the container is running** based on the current container runtime.

- **Docker runtime**: Use `docker commit` to save the current container as a new image, and then label the image or export it as a `.tar` file as required.

  ```bash
  docker commit <Container ID or name> <Image name>:<Tag>
  # (Optional) Export the image as an offline package
  docker save -o mindie-motor-vllm-dev.tar <Image name>:<tag>
  ```

- **containerd runtime**: containerd does not have a command equivalent to the `docker commit` command for converting a container into an image. You need to use Docker or a tool with the commit capability to save the container as an image on another node and export it as a `.tar` file. Then, use `ctr -n k8s.io images import` or `nerdctl -n k8s.io load -i` to import the image on the containerd node. Alternatively, you can install MindIE-PyMotor in the image using Dockerfile during the build phase and then export the image for containerd to use.

### Loading a Custom Image in Different Environments

In an actual cluster, you need to load the image on all nodes where the Controller, Coordinator, and P/D Server pods are running. The loading method is the same as that described in section 2.1. You can select a proper command based on the runtime type. For example:

- Docker node:

  ```bash
  docker load -i mindie-motor-vllm-dev.tar
  ```

- Node that uses containerd (the `ctr` command is used as an example):

  ```bash
  ctr -n k8s.io images import mindie-motor-vllm-dev.tar
  ```

## Deployment Directory Structure

Upload the **examples** directory in this repository to the master node of the Kubernetes cluster. The main directory structure related to the PD disaggregation deployment of is as follows:

```text
examples/
├── deployer/                  # Directory of the deployment tool
│   ├── deploy.py              # Deployment entry script
│   ├── delete.sh              # Uninstallation script
│   ├── show_log.sh            # Log viewing script
│   ├── README.md              # Instructions for the deployment tool
│   ├── yaml_template/         # Kubernetes YAML template
│   ├── startup/               # Startup script
│   │   ├── boot.sh            # Startup script in the container
│   │   ├── common.sh          # Script for setting common environment variables
│   │   ├── hccl_tools.py      # Ranktable generation
│   │   ├── mooncake_config.py # Mooncake configuration generation
│   │   └── roles/             # Script for setting environment variables for each component
│   ├── probe/                 # Probe script
│   ├── log_collect/           # Log collection
│   │   ├── log_config.ini     # Log collection configuration (Before using show_log, configure name_space and other parameters)
│   │   └── log_monitor.py     # Log pulling script (started by show_log.sh)
│   └── output_yamls/          # Output directory of the generated YAML files
└── infer_engines/             # Configuration examples of each engine
    └── vllm/                  # vLLM engine configuration
        ├── user_config.json   # User configuration for quick startup
        ├── env.json           # Environment variable configuration for quick start
        └── models/            # Specific model configuration
```

- The configuration file is located in the `examples/infer_engines/` directory (for example, `examples/infer_engines/vllm/user_config.json`). Select the corresponding configuration based on the engine type and model.
- For details about how to use the deployment tool, see `examples/deployer/README.md`.

## Configuring `user_config.json`

Edit the configuration in the corresponding model configuration file (for example, `examples/infer_engines/vllm/models/deepseek/v3_1/user_config.json`) under `examples/infer_engines/vllm/user_config.json` or `examples/infer_engines/vllm/models/`. Use the actual configuration. This file uses the JSON root structure. The root node contains `version` (fixed at `"v2.0"`) and the configuration objects of each module. The following describes the configuration items that require special attention in the PD disaggregation scenario by module.

### `motor_deploy_config` (Deployment and Resources)

`motor_deploy_config` contains deployment and resource configurations.

**Configuration example** (1P1D, 16 cards per pod; `tls_config` is optional. For details about the structure, see section 4.6):

```json
"motor_deploy_config": {
  "p_instances_num": 1,
  "d_instances_num": 1,
  "single_p_instance_pod_num": 1,
  "single_d_instance_pod_num": 1,
  "p_pod_npu_num": 16,
  "d_pod_npu_num": 16,
  "image_name": "",
  "job_id": "mindie-motor",
  "hardware_type": "800I_A3",
  "weight_mount_path": "/mnt/weight/",
  "tls_config": { ... }
}
```

**Configuration items**

| Configuration Item| Type| Description|
|--------|------|------|
| p_instances_num | int | Number of P instances, ≥ 1|
| d_instances_num | int | Number of D instances, ≥ 1|
| single_p_instance_pod_num | int | Number of pods corresponding to a single P instance, ≥ 1.|
| single_d_instance_pod_num | int | Number of pods corresponding to a single D instance, ≥ 1.|
| p_pod_npu_num | int | Number of NPUs occupied by a single P instance pod.|
| d_pod_npu_num | int | Number of NPUs occupied by a single D instance pod.|
| image_name | string | Enter the name of the inference image prepared or loaded in [2. Preparing an Image](#preparing-an-image) in this document. (The running environment, such as MindIE-PyMotor and vLLM, must be included.)|
| job_id | string | Deployment task name, which is also used as the Kubernetes namespace, for example, `mindie-motor`.|
| hardware_type | string | Hardware type: `800I_A2` or `800I_A3`|
| weight_mount_path | string | Path for mounting the model weight on the host machine. The value of `model_path` in the container must be the same as the mount path, for example, `"/mnt/weight/"`.|
| tls_config | object | (Optional) TLS configuration. For details about the complete structure and example, see Section 4.6.|

### `motor_controller_config`/`motor_coordinator_config`

`deploy.py` merges the sub-configurations of the Controller and Coordinator in `user_config.json` into the component runtime configuration. The default values in the code are used first, and then the configuration items here overwrite the default values. The configuration file monitored by a component can be modified to make the modification take effect dynamically. For more configurable items and full parameter descriptions, see [Parameters in user_config](./config_reference.md).

**Configuration example** (consistent with the structure of `user_config.json` in the repository):

```json
"motor_controller_config": {
  "standby_config": {
    "enable_master_standby": false
  }
},
"motor_coordinator_config": {
  "standby_config": {
    "enable_master_standby": false
  },
  "request_limit": {
    "single_node_max_requests": 4096,
    "max_requests": 10000
  }
}
```

**Configuration items**

`motor_controller_config`: The example in the repository contains only the master/standby switch.

| Configuration Item| Type| Description|
|--------|------|------|
| standby_config.enable_master_standby | bool | Whether to enable the master/standby mode for the Controller.|

`motor_coordinator_config`: includes the master/standby switch and request throttling.

| Configuration Item| Type| Description|
|--------|------|------|
| standby_config.enable_master_standby | bool | Whether to enable the master/standby mode for the Coordinator.|
| request_limit.single_node_max_requests | int | Maximum number of requests on a single node.|
| request_limit.max_requests | int | Maximum number of global requests|

### `motor_nodemanger_config`

Used for special configuration of the NodeManager component. In `user_config.json` in the repository, this object is empty `{}`. In the PD disaggregation scenario, this parameter does not need to be configured. For details about all parameters, see [Parameters in user_config](./config_reference.md#4-motor_nodemanger_config). Note that the field name is `motor_nodemanger_config` (the spelling is the same as that in the repository).

### `motor_engine_prefill_config`/`motor_engine_decode_config` (P/D engine)

The structures of the two parameters are similar, and both require the specification of `engine_type`, `model_config` and `engine_config`.

#### Configuration example (Qwen3-8B, MooncakeLayerwiseConnector)

```json
"motor_engine_prefill_config": {
  "engine_type": "vllm",
  "model_config": {
    "model_name": "qwen3-8B",
    "model_path": "/mnt/weight/qwen3_8B",
    "npu_mem_utils": 0.9,
    "prefill_parallel_config": {
      "dp_size": 2,
      "tp_size": 2,
      "pp_size": 1,
      "enable_ep": false,
      "dp_rpc_port": 9000,
      "world_size": 4
    }
  },
  "engine_config": {
    "enforce-eager": true,
    "max_model_len": 2048,
    "kv_transfer_config": {
      "kv_connector": "MooncakeLayerwiseConnector",
      "kv_buffer_device": "npu",
      "kv_role": "kv_producer",
      "kv_parallel_size": 1,
      "kv_port": "20001",
      "engine_id": "0",
      "kv_rank": 0,
      "kv_connector_module_path": "vllm_ascend.distributed.mooncake_layerwise_connector",
      "kv_connector_extra_config": {}
    }
  }
},
"motor_engine_decode_config": {
  "engine_type": "vllm",
  "model_config": {
    "model_name": "qwen3-8B",
    "model_path": "/mnt/weight/qwen3_8B",
    "npu_mem_utils": 0.9,
    "decode_parallel_config": {
      "dp_size": 2,
      "tp_size": 2,
      "pp_size": 1,
      "enable_ep": false,
      "dp_rpc_port": 9000,
      "world_size": 4
    }
  },
  "engine_config": {
    "max_model_len": 2048,
    "kv_transfer_config": { ... }
  }
}
```

The following describes the preceding configuration items one by one.

#### Root node configuration items

| Configuration Item| Type| Description|
|--------|------|------|
| engine_type | string | Engine type, for example, `vllm`.|
| model_config | object | Model-related configuration. For details, see the following table.|
| engine_config | object | Engine-related configurations, including KV transmission and native engine parameters.|

#### Configuration items in `model_config`

| Configuration Item| Type| Description|
|--------|------|------|
| model_name | string | Model name, for example, `qwen3-8B`.|
| model_path | string | Path to the model weight in the container, which must be the same as the path mounted to `weight_mount_path`, for example, `/mnt/weight/qwen3_8B`.|
| npu_mem_utils | float | Upper limit of the NPU memory usage. The value ranges from 0 to 1, for example, `0.9`.|
| prefill_parallel_config | object | Configuration on the prefill side (appears only in prefill). For details, see the following table.|
| decode_parallel_config | object | Configuration on the decode side (appears only in decode). For details, see the following table.|

#### Configuration items in `prefill_parallel_config`/`decode_parallel_config`

| Configuration Item| Type| Description|
|--------|------|------|
| dp_size | int | Data parallel size.|
| tp_size | int | Tensor parallel size.|
| pp_size | int | Pipeline parallel size|
| enable_ep | bool | Whether to enable EP|
| dp_rpc_port | int | RPC port on the DP side|
| world_size | int | Total number of cards. For vLLM, the value is `dp_size * tp_size * pp_size`. For SGLang, the value varies depending on whether `enable-dp-attention` is enabled. If it is enabled, `dp_size` does not need to be multiplied.|

#### Configuration items in `engine_config`

`engine_config` includes the following configurations: `kv_transfer_config` (which governs KV transfer and may contain fields such as `kv_connector_extra_config`) and native engine parameters. Except for the `kv_transfer_config` structure described below, other items (including subfields in `kv_connector_extra_config` and other keys) should be filled in based on the native parameters of the selected engine (such as vLLM). For details, see the corresponding engine documentation.

| Configuration Item| Type| Description|
|--------|------|------|
| kv_transfer_config | object | KV transfer configuration, which is critical for PD collaboration. For details, see the following table.|
| Other keys| - | Native parameters of the engine (such as `max_model_len` and `enforce-eager` of vLLM). Set these parameters based on the engine documentation.|

#### Configuration items in `kv_transfer_config`

| Configuration Item| Type| Description|
|--------|------|------|
| kv_connector | string | KV connector type, for example, `MooncakeLayerwiseConnector`.|
| kv_buffer_device | string | KV buffer device, for example, `npu`.|
| kv_role | string | KV role. The value is `kv_producer` for prefill and `kv_consumer` for decode.|
| kv_parallel_size | int | KV parallel size.|
| kv_port | string | KV port.|
| engine_id | string | Engine ID.|
| kv_rank | int | KV rank |
| kv_connector_module_path | string | Connector module path.|
| kv_connector_extra_config | object | Additional configuration. For details, see the following table.|

#### Configuration items in `kv_connector_extra_config`

| Configuration Item| Type| Description|
|--------|------|------|
| prefill | object | Additional configurations on the prefill side, such as `dp_size` and `tp_size`.|
| decode | object | Additional configuration on the decode side, such as `dp_size` and `tp_size`.|

Prefill/Decode subfields:

| Configuration Item| Type| Description|
|--------|------|------|
| dp_size | int | Data parallel size.|
| tp_size | int | Tensor parallel size.|

> **NOTE**
> 
>- `kv_connector_extra_config` fields such as `dp_size` and `tp_size` for prefill/decode typically do not require manual configuration. Motor automatically updates them at service startup based on the settings in `prefill_parallel_config`/`decode_parallel_config`.
>- If KV pooling is required, switch to MultiConnector and refer to the [KV Pooling Deployment Guide](../KV_pool_deployment_guide.md) to modify `user_config.json` accordingly, and use it together with `deploy.py`.

### (Optional) `kv_cache_pool_config`

This parameter needs to be configured only when KV cache pooling is enabled. For details, see [KV Pooling Deployment Guide](../KV_pool_deployment_guide.md). If only PD disaggregation is used and pooling is not enabled, retain the default structure in the repository.

**Configuration example** (consistent with `user_config.json` in the repository)

```json
"kv_cache_pool_config": {
  "metadata_server": "P2PHANDSHAKE",
  "protocol": "ascend",
  "device_name": "",
  "alloc_in_same_node": true,
  "global_segment_size": "1GB"
}
```

**Configuration items**

| Configuration Item| Type| Description|
|--------|------|------|
| metadata_server | string | Metadata service type, for example, `"P2PHANDSHAKE"`.|
| protocol | string | Protocol, for example, `"ascend"`|
| device_name | string | Device name, which can be an empty string.|
| alloc_in_same_node | bool | Whether to allocate on the same node|
| global_segment_size | string | Global segment size, for example, `"1GB"`.|

### (Optional) `tls_config`

`motor_deploy_config.tls_config` shares the same structure as `user_config.json` in the repository. It contains four types of TLS configuration objects, each with an identical structure. For details about how to generate a certificate, see the certificate generation part in [examples/enable_tls/README.md](../../../../examples/features/http/enable_tls/README.md).

**Configuration example** (complete structure, which can be filled in `motor_deploy_config`):

```json
"tls_config": {
  "infer_tls_config": {
    "tls_enable": false,
    "ca_file": "/usr/local/Ascend/pyMotor/conf/security/infer/ca.pem",
    "cert_file": "/usr/local/Ascend/pyMotor/conf/security/infer/cert.pem",
    "key_file": "/usr/local/Ascend/pyMotor/conf/security/infer/nopass.cert.key.pem",
    "passwd_file": "/usr/local/Ascend/pyMotor/conf/security/infer/key_pwd.txt",
    "tls_crl": ""
  },
  "mgmt_tls_config": {
    "tls_enable": false,
    "ca_file": "/usr/local/Ascend/pyMotor/conf/security/mgmt/ca.pem",
    "cert_file": "/usr/local/Ascend/pyMotor/conf/security/mgmt/cert.pem",
    "key_file": "/usr/local/Ascend/pyMotor/conf/security/mgmt/nopass.cert.key.pem",
    "passwd_file": "/usr/local/Ascend/pyMotor/conf/security/mgmt/key_pwd.txt",
    "tls_crl": ""
  },
  "etcd_tls_config": {
    "tls_enable": false,
    "ca_file": "/usr/local/Ascend/pyMotor/conf/security/etcd/ca.pem",
    "cert_file": "/usr/local/Ascend/pyMotor/conf/security/etcd/cert.pem",
    "key_file": "/usr/local/Ascend/pyMotor/conf/security/etcd/nopass.cert.key.pem",
    "passwd_file": "/usr/local/Ascend/pyMotor/conf/security/etcd/key_pwd.txt",
    "tls_crl": ""
  },
  "grpc_tls_config": {
    "tls_enable": false,
    "ca_file": "/usr/local/Ascend/pyMotor/conf/security/clusterd/ca.pem",
    "cert_file": "/usr/local/Ascend/pyMotor/conf/security/clusterd/cert.pem",
    "key_file": "/usr/local/Ascend/pyMotor/conf/security/clusterd/nopass.cert.key.pem",
    "passwd_file": "/usr/local/Ascend/pyMotor/conf/security/clusterd/key_pwd.txt",
    "tls_crl": ""
  }
}
```

**Configuration items**

- `infer_tls_config`: TLS on the inference plane
- `mgmt_tls_config`: TLS on the management plane
- `etcd_tls_config`: etcd TLS
- `grpc_tls_config`: gRPC TLS for cluster communication (The certificate path is usually `.../security/clusterd/`.)

The fields of TLS configuration objects are as follows:

| Configuration Item| Type| Description|
|--------|------|------|
| tls_enable | bool | Whether to enable TLS. If the value is `false`, TLS is disabled and no certificate is required.|
| ca_file | string | CA certificate path.|
| cert_file | string | Server certificate path.|
| key_file | string | Private key path.|
| passwd_file | string | Path to the private key password file.|
| tls_crl | string | Path to the certificate revocation list.|

You are advised to enable TLS in the production environment to ensure communication security.

## Configuring `env.json`

`env.json` injects environment variables into components. Its path is specified by `motor_deploy_config.env_path` in `user_config.json` (e.g., `./conf/env.json`). `deploy.py` reads this file and writes the corresponding sections into functions within `boot_helper/boot.sh`, such as `set_common_env`, `set_controller_env`, `set_coordinator_env`, `set_prefill_env`, `set_decode_env`, and `set_kv_pool_env`. These functions are then sourced during container startup.

**Configuration example** (typical structure):

```json
{
  "version": "2.0.0",
  "motor_common_env": {
    "CANN_INSTALL_PATH": "/usr/local/Ascend"
  },
  "motor_controller_env": {},
  "motor_coordinator_env": {},
  "motor_engine_prefill_env": {
    "HCCL_BUFFSIZE": 200,
    "PYTORCH_NPU_ALLOC_CONF": "expandable_segments:True",
    "HCCL_OP_EXPANSION_MODE": "AIV",
    "OMP_PROC_BIND": "false",
    "OMP_NUM_THREADS": 100,
    "ASCEND_BUFFER_POOL": "4:8"
  },
  "motor_engine_decode_env": {
    "HCCL_BUFFSIZE": 200,
    "PYTORCH_NPU_ALLOC_CONF": "expandable_segments:True",
    "HCCL_OP_EXPANSION_MODE": "AIV",
    "OMP_PROC_BIND": "false",
    "OMP_NUM_THREADS": 100,
    "ASCEND_BUFFER_POOL": "4:8"
  },
  "motor_kv_cache_pool_env": {}
}
```

**Configuration items**

- `motor_common_env`: (for example, the CANN installation path) shared by all components.
- `motor_engine_prefill_env`/`motor_engine_decode_env`: NPU, HCCL, and OMP environment variables of the P/D instance, which can be tuned based on the machine and model type.

Common environment variables (P/D engine, corresponding to the preceding configuration example) are as follows:

| Variable| Description| Default Value|
|--------|------|----------|
| CANN_INSTALL_PATH | CANN installation path (`motor_common_env`)| /usr/local/Ascend |
| HCCL_BUFFSIZE | HCCL buffer size| 200 |
| PYTORCH_NPU_ALLOC_CONF | NPU graphics memory allocation policy| expandable_segments:True |
| HCCL_OP_EXPANSION_MODE | Location for expanding the orchestration of the communication algorithm| AIV |
| OMP_PROC_BIND | OpenMP thread binding| false |
| OMP_NUM_THREADS | Number of OpenMP threads| 100 |
| ASCEND_BUFFER_POOL | Buffer pool configuration| 4:8 |

After saving the changes, there is no need to manually modify `boot.sh`; the environment variables above will be regenerated and injected the next time `deploy.py` is run.

## Performing Deployment (`deploy.py`)

### Security and Permissions

- It is recommended that the deployment script be executed by the **Kubernetes cluster administrator** to prevent script or configuration tampering, which may cause arbitrary command execution or container escape risks.
- Strictly control the write, update, and deletion permissions on MindIE-related ConfigMaps (such as `motor-config`). It is recommended that the permission on the installation directory be set to `750` and the permission on files be set to `640`, and that namespaces and RBAC be used for constraints.
- When modifying the deployment template, use secure images (non-root and secure pod context) and mount secure paths (avoid soft links, system‑critical directories, and sensitive service paths).

>[!NOTE]NOTE
>When requests are sent faster than they are processed, the Coordinator caches the unprocessed requests, leading to increased memory usage. As a result, request sending may be terminated because the memory usage reaches the upper limit. In this case, increase the `memory` parameters under `requests` and `limits` in either `examples/deployer/yaml_template/coordinator_template.yaml` (for multi‑deployment scenarios) or `examples/deployer/yaml_template/infer_service_template.yaml` (for CRD scenarios), as appropriate.
>
>- `requests.memory`: minimum memory required for Coordinator running.
>- `limits.memory`: maximum available memory of the Coordinator.
>
>To ensure that the Coordinator can stably obtain the preceding memory, you are advised to set the two parameters to the same value. The recommended memory size based on the number of backlogged requests is as follows:
>
>- About 10,000 backlogged requests: `4Gi`
>- About 20,000 backlogged requests: `8Gi`
>- About 40,000 backlogged requests: `16Gi`
>- About 90,000 backlogged requests: `24Gi`

### Prerequisites

- Kubernetes, MindCluster, NPU driver, firmware, and image have been installed, and the weight path has been configured.
- A namespace with the same name as `job_id` has been created. For example:

  ```bash
  kubectl create namespace mindie-motor
  ```

- The model weights on the host are located at the path specified by `weight_mount_path` in `user_config.json` (e.g., `/mnt/weight/`).

### Deployment Commands

Run the following commands in the `examples/deployer` directory. Two configuration methods are supported:

**Method 1 (recommended): Specify the configuration directory.** The directory must contain `user_config.json` and `env.json`.

```bash
cd examples/deployer
python3 deploy.py --config_dir ../infer_engines/vllm
```

**Method 2: Specify the configuration file path separately.** Both `--user_config_path` and `--env_config_path` must be specified.

```bash
cd examples/deployer
python3 deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
```

You can also use the abbreviations `--config` and `--env`.

Main options:

- `--config_dir` or `--dir`: directory containing the configuration files. It must include `user_config.json` and `env.json` (recommended, e.g., `../infer_engines/vllm`).
- `--user_config_path` or `--config`: user configuration file path. Must be specified together with `--env`.
- `--env_config_path` or `--env`: path to the environment configuration file. Must be specified together with `--config`.
- `--update_config`: Only the ConfigMap (`motor-config`) is updated, and the Deployment is not applied again.
- `--update_instance_num`: Scale in or out the number of instances based on the configuration.

`--update_config` only supports modifications to trustlisted configuration items. The script compares the current `user_config.json` against the deployed `motor-config` baseline in the cluster field by field. If any non-trustlisted field changes are detected, it will immediately report an error and reject the update. Currently, the trustlist includes the following types:

- Module log levels: `motor_controller_config.logging_config.log_level`, `motor_coordinator_config.logging_config.log_level`, `motor_nodemanger_config.logging_config.log_level`
- Controller observability configuration: `motor_controller_config.observability_config`
- Coordinator exception handling configuration: `motor_coordinator_config.exception_config`
- Coordinator timeout configuration: `motor_coordinator_config.timeout_config`

For details on which fields can be modified and which newly added fields under the trustlist configuration will be blocked, refer to [--update_config Trustlist](./update_config_whitelist.md).

Except the trustlist, other configuration items (including deployment resources, number of instances, model configuration, TLS, active/standby mode, and rate limiting) cannot be modified using `--update_config`. If scaling is required, use `--update_instance_num`. If other configurations need to be changed, perform the deployment again according to the normal deployment process.

Example:

```bash
# Use the configuration directory (recommended)
python3 deploy.py --config_dir ../infer_engines/vllm

# Specify configuration files separately (both --user_config_path and --env_config_path must be specified)
python3 deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json

# Update configurations only
python3 deploy.py --config_dir ../infer_engines/vllm --update_config

# Scale instances
python3 deploy.py --config_dir ../infer_engines/vllm --update_instance_num
```

For more usage methods, see `examples/deployer/README.md`.

`deploy.py` performs the following steps in sequence:

1. Read `user_config.json` and `env.json`.
2. Based on `motor_deploy_config`, generate Deployment YAMLs for the Controller, Coordinator, and each P/D instance, and output them to `output_yamls/`.
3. Write environment variables from `env.json` into the corresponding functions in each script under `startup/`.
4. Apply the generated YAMLs using `kubectl apply -f ... -n <job_id>` to bring up the task pods.

### Viewing the Cluster Status and Logs

View the pod list (replace `<job_id>` with the actual namespace, for example, `mindie-motor`).

```bash
kubectl get pods -n <job_id>
```

The names of pods and Deployments in the command output may vary depending on the template and `engine_type`. You are not advised to determine the role based only on a fixed prefix. You can identify them as follows:

- Controller/Coordinator: Check the generated YAML in `output/deployment/` (or run `kubectl get deployments -n <job_id>`) and use the actual Deployment/Service names.
- P/D Server: The current `engine_type` supports `vllm`, `mindie-llm`, and `sglang`. Using `engine_type=vllm` as an example, `deploy.py` generates Deployments like `vllm-p0`, `vllm-d0` (with incrementing indices). Other types follow the same pattern, with the base Deployment name varying according to `engine_type`.

Pod status `Running` only indicates that the pod has been successfully scheduled and started. Whether the business is ready still requires further confirmation via logs.

**Viewing Logs and Troubleshooting**

You can use any of the following methods to view cluster logs or locate faults:

**Method 1: using `show_log.sh` to collect logs in a unified manner**

1. (Required) Configure `log_config.ini`. `show_log.sh` starts `log_monitor.py` in the `log_collect` directory. This script reads the `[LogSetting]` configuration from `examples/deployer/log_collect/log_config.ini` in the same directory. The `name_space` field is empty by default and must be set to the Kubernetes namespace where the workload is actually deployed. This value must match the `<job_id>` used when viewing Pods or running `kubectl` commands as described in this document. If left blank, `show_log.sh` outputs an error to stderr and exits immediately, without starting `log_monitor.py` (see Section 6 in *FAQs*). If the filled-in value does not match the actual namespace, `kubectl get pods` and `kubectl logs` will target the wrong namespace, causing log collection to fail or return empty results. In the same file, adjust `out_path` (log output directory), `max_log_size` (max size per log file), and `backup_count` (number of rotated backups) as needed—see the script for exact units and semantics.

2. Execute the collection script. Run `show_log.sh` in the `examples/deployer` directory to retrieve and view logs.

   ```bash
   cd examples/deployer
   bash show_log.sh
   ```

3. **About collected log files**:
   - Storage directory: Logs are collected under the `out_path` directory specified in `log_config.ini`, with a timestamp folder (e.g., `20260328_102430`) created based on the current system local time.
   - File naming convention: Log files for a single Pod are named as `<pod_name>_<node_name>_<retry_count>.log`, which directly includes the Kubernetes node name (e.g., `vllm-0-controller-0-xxxx_node01_0.log`), facilitating cross-node troubleshooting.
   - Log rotation (auto-split) mechanism: When the collected log size exceeds `max_log_size` (default 10 MB) configured in `log_config.ini`, rotation is triggered. The current log file is renamed with a `.1`, `.2`, etc. suffix (e.g., `xxx_0.log.1` and `xxx_0.log.2` denote earlier log segments), and a new `.log` file is created to continue logging. They all belong to the log stream of a single pod lifetime. The number of backups saved during rotation is limited by `backup_count`.

**Method 2: fallback solution (viewing logs of a single Pod)**

If you only want to quickly view the standard output of a specific pod, you can directly run the `kubectl` command:

```bash
kubectl logs <pod_name> -n <job_id>
```

*Example: `kubectl logs mindie-server-p0-xxx -n mindie-motor`*

**Method 3: accessing the container for troubleshooting**

To view the internal status of a container or run debugging commands, run the following command:

```bash
kubectl exec -it <pod_name> -n <job_id> -- bash
```

**Confirm the mapping between P/D instances and Pods:**

Based on the IP addresses and names in the Pod list, you can determine which Pods correspond to P instances and which Pods correspond to D instances. After confirming that no error is reported for each component and the inference service is ready, you can send an inference request for verification by referring to the next section.

## Sending an Inference Request

Once the service is ready, you can verify that it has started correctly by sending an inference request. For example, use the `/v1/chat/completions` endpoint (see [Service Interfaces](../../api_reference/service_interface.md) for other APIs.) The inference entry is the port (`31015` by default) exposed by the Coordinator. Replace `<IP>` with the actual access address (for example, the NodePort/LoadBalancer of the Coordinator Service or the IP address of the host machine). If TLS has been enabled (see section 4.6), use `https` and configure the client certificate.

```bash
curl -X POST http://<IP>:31015/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3",
    "prompt": "who are you?",
    "max_tokens": 36,
    "stream": true
  }'
```

If the response is `{"detail":"Service is not available"}`, the service is not yet ready; retry later. If streaming JSON is returned, the inference is normal.

>[!NOTE]NOTE
>HTTPS is recommended because HTTP is insecure. For details about how to enable the certificate and TLS configuration, see [4.6 (Optional) `tls_config`](#optional-tls_config).

## Uninstallation

In the `examples/deployer` directory, run `delete.sh` to remove the `K8s ConfigMap` under the current `job_id` namespace, delete the YAMLs previously applied from `output_yamls`, and clean up the environment variable functions injected by `deploy.py` in `startup/`.

```bash
cd examples/deployer
bash delete.sh <Namespace>
```

Example: `bash delete.sh mindie-motor`

>[!NOTE]NOTE
>
>- Replace the namespace with the actual name you created (for example, the namespace corresponding to `job_id`).
>- `delete.sh` removes the `motor-config` ConfigMap from the specified namespace, deletes all applied YAML files under `output_yamls/`, and cleans up the `set_*_env` functions injected into `startup/` by `deploy.py`. The uninstallation script must be executed within the `examples/deployer` directory; otherwise, it will fail to locate the `output_yamls` path and throw an error.

## Troubleshooting and Precautions

- **Service not ready**: If the inference API returns `{"detail":"Service is not available"}`, the P/D or Coordinator is not ready. Wait for a while and try again. Check the logs of each Pod to ensure that no startup error occurs.
- **Image and weight**: Ensure that `image_name` can be successfully pulled within the cluster, and that `weight_mount_path` exists on the host machine.
- **Deployment failure**: If the deployment fails, uninstall the cluster by referring to Section 8, check and modify the configuration, and deploy the cluster again.
- **Load weight timeout**: In vLLM v0.13.0, the weight loading timeout cannot be configured via environment variables or settings. As a result, loading weights that take longer than 10 minutes will trigger a timeout error, though this does not affect program execution. This issue will be resolved in vLLM v0.14.0.
- **Instance rescheduling constraint**: Instance rescheduling capability relies on MindCluster. If a P/D instance has multiple Pods and one Pod is deleted directly, it will not trigger MindCluster's fault handling process, and thus instance rescheduling will not be initiated.
- **Impact of the Prefix Cache feature (enabled by default) on the performance test**: Prefix Cache is used to reuse computed KV Cache across requests that share the same prompt prefix, improving inference performance. To obtain baseline inference profile data (without prefix caching acceleration), disable the Prefix Cache feature by adding `"no-enable-prefix-caching": true` (for vLLM) or `"disable_radix_cache": true` (for SGLang) under `"engine_config"` in `user_config.json`.
