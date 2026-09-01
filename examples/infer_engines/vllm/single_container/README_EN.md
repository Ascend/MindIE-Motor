# Single-Container PD Disaggregation Deployment Guide

## Feature Introduction

MindIE Motor supports starting a PD disaggregation service within a single container: Coordinator/Controller/PD instances.

## Deployment Process

After modifying the `user_config.json` configuration file, MindIE Motor can complete service deployment through the `deploy.py` script. The specific process is as follows.

1. Configure `user_config.json`.

   Using the example `user_config.json` in [Quick Start](../../../../docs/en/user_guide/quick_start.md) as the reference baseline, the relevant adaptation points are as follows:

    ```json
      "motor_deploy_config": {
        ...
        "deploy_mode": "single_container"
      },
      "motor_controller_config": {
        ...
        "fault_tolerance_config": {
          "enable_fault_tolerance": false,
          "enable_scale_p2d": true,
          "enable_token_reinference": true
        },
        "api_config": {
          "controller_api_port": 2026
        }
      },
      "motor_coordinator_config": {
        ...
        "api_config": {
          "coordinator_api_infer_port": 1025,
          "coordinator_api_mgmt_port": 1026,
          "coordinator_obs_port": 1027
        }
      },
      "motor_engine_prefill_config": {
        ...
        "motor_nodemanger_config": {
          "api_config": {
            "node_manager_port": 3026
          }
        },
        "model_config": {
          ...
          "prefill_parallel_config": {
            ...
            "dp_rpc_port": 9000
          }
        },
        "engine_config": {
          ...
          "kv_transfer_config": {
            ...
            "kv_port": "20001",
            ...
          }
        }
      },
      ...
    }
    ```

   >[!NOTE]NOTE
   >
   >- The RAS feature is not supported. Set `enable_fault_tolerance` to `false`.
   >- Ensure that the ports of each component do not overlap:
   >
   >   - For `ode_manager_port`/`dp_rpc_port`/`lookup_rpc_port`, ensure that each instance does not overlap. During actual deployment, they are automatically offset by 1 in the order of P first and then D, with a value range of [base port, base port + total number of instances). The base port of `dp_rpc_port`/`lookup_rpc_port` is determined by the prefill configuration.
   >   - For `kv_port`, ensure that each dp group does not overlap. During actual deployment, they are automatically offset by the number of cards in the dp group in the order of P first and then D, with a value range of [kv_port, kv_port + total number of cards).

2. Deploy the service.

  The current directory provides the `user_config` template — `user_config.json`. Execute the following command in the `examples/deployer` directory to complete service deployment:

    ```bash
    cd examples/deployer
    # (Recommended) Method 1: Specify the configuration directory
    python deploy.py --config_dir ../infer_engines/vllm/single_container

    # Method 2: Specify the configuration file separately
    python deploy.py --user_config_path ../infer_engines/vllm/single_container/user_config.json --env_config_path ../infer_engines/vllm/single_container/env.json
    ```
