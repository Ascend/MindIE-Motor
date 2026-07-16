# MindIE Motor配置自动生成指导

MindIE Motor的一键部署工具可以实现“将vllm-ascend社区的部署脚本转换为Motor部署配置”，以降低操作成本并保证与下游推理引擎配置一致。

**同级目录下已准备常用部署模型的配置示例**，例如：[deepseek v4 flash模型配置示例](./deepseek_v4_flash)

---

## 目录简介

配置生成脚本存放于[examples/deployer/config_tool/](../../../deployer/config_tool/)目录下，各文件功能如下。

```bash
examples/deployer/config_tool/
├── vllm_to_motor.py                    # 配置转换脚本
├── run_dp_template_hybrid.sh           # 用户粘贴：vLLM-ascend 混部启动脚本
├── run_dp_template_prefill.sh          # 用户粘贴：vLLM-ascend P 实例启动脚本
├── run_dp_template_decode.sh           # 用户粘贴：vLLM-ascend D 实例启动脚本
└── output_config/                      # 生成的Motor配置内容
    ├── user_config.json
    └── env.json
```

---

## 注意事项

1. 在执行使用方法的第2步[在vllm-ascend社区查找模型部署脚本]时，请确保部署镜像中的vllm-ascend版本和社区版本一致，**避免新配置应用于旧代码的情况**。
2. 生成的Motor配置仅给出一种可行的模型切分示例，**用户可根据集群服务器数量，调整服务占用的服务器数量和模型划分策略，调整模型切分策略时关注以下参数即可**。

      | 配置项 | 取值类型 | 取值范围 | 配置说明 |
      | --- | --- | --- | --- |
      | p_instances_num | int | ≥1 | Prefill 实例数量。 |
      | d_instances_num | int | ≥1| Decode 实例数量。 |
      | single_p_instance_pod_num | int | ≥1 | 1 个 P 实例拆成几个 Pod。|
      | single_d_instance_pod_num | int | ≥1 | 1 个 D 实例拆成几个 Pod。 |
      | p_pod_npu_num | int | ≥1，单 Pod 最大 16 卡 | 每个 P Pod 使用的 NPU 卡数。 |
      | d_pod_npu_num | int | ≥1，单 Pod 最大 16 卡 | 每个 D Pod 使用的 NPU 卡数。 |
      | data_parallel_size | int | ≥1 | 数据并行（DP）数 |
      | tensor_parallel_size | int | ≥1 | 张量并行（TP）数 |

      1个P实例占用的NPU卡数 = single_p_instance_pod_num（占用几个pod，跨几机） × p_pod_npu_num （每个pod占用的NPU数）= data_parallel_size × tensor_parallel_size

3. 生成的Motor配置仅支撑基础推理服务成功部署，**Motor特性调整（例如：主备倒换、KV 亲和性调度、服务限流）需要用户手动修改配置**。
4. 当前不支持单容器PD分离和单容器PD混部的场景。

---

## 使用方法

1. 进入配置脚本主目录。

    ```bash
    cd examples/deployer/
    ```

2. 在vllm-ascend社区查找部署脚本。

    进入[vllm-ascend模型部署指导网址](https://github.com/vllm-project/vllm-ascend/tree/main/docs/source/tutorials/models)，基于模型选择对应文档，在文档的`Online Service Deployment`小节（通常为第5小节）找到模型部署脚本（通常命名为`run_dp_template.sh`），重点关注：**pd混部部署脚本**（小标题名称为`Single-Node Online Deployment`）和**PD分离部署脚本**（小标题名称为`Multi-Node PD Separation Deployment`）。

    **举例**：

      [dsv4 flash部署指导](https://github.com/vllm-project/vllm-ascend/blob/main/docs/source/tutorials/models/DeepSeek-V4-Flash.md#51-single-node-online-deployment)下。

    - 5.1小节的"A3 series"小标题下，即为**PD混部部署脚本**，内容如下：

      ```bash
      export OMP_PROC_BIND=false
      export OMP_NUM_THREADS=10
      ...

      vllm serve /root/.cache/modelscope/hub/models/vllm-ascend/DeepSeek-V4-Flash-w8a8-mtp \
          --max-model-len 1048576 \
          --max-num-batched-tokens 10240 \
          ...
      ```

    - 5.2.1小节的"run_dp_template.sh"的脚本内容，即为**PD分离部署脚本**，以P实例脚本为例：

      ```bash
      nic_name="xxxx" # change to your own nic name
      local_ip=xx.xx.xx.1 # change to your own ip

      export HCCL_IF_IP=$local_ip
      export GLOO_SOCKET_IFNAME=$nic_name
      ...

      vllm serve /root/.cache/modelscope/hub/models/vllm-ascend/DeepSeek-V4-Flash-w8a8-mtp \
          --host 0.0.0.0 \
          --port $2 \
          ...
      ```

    >[!NOTE]说明
    >一个实例占用多个服务器的场景下，vllm-ascend社区可能为一个实例的部署提供多份脚本（分别对应多台服务器），这些部署脚本的配置没有明显差异，仅需要关注其中一份脚本。
    >
    >例如：[qwen3-235B模型部署指导](https://github.com/vllm-project/vllm-ascend/blob/main/docs/source/tutorials/models/Qwen3-235B-A22B.md#52-multi-node-pd-separation-deployment)的5.2小节中，同时存在Decode node 0和Decode node 1，在使用时选取任意一份作为D实例部署脚本即可。

3. 拷贝vllm-ascend模型部署脚本至examples/deployer/config_tool/目录。

    run_dp_template_prefill.sh、run_dp_template_decode.sh和run_dp_template_hybrid.sh文件用于保存vllm-ascnd部署脚本，这些文件均保存于examples/deployer/config_tool/目录下。

    - **场景一**：通过Motor部署PD分离服务。

       无需额外修改，将上述网址中PD分离部署脚本直接拷贝至run_dp_template_prefill.sh(P节点)和run_dp_template_decode.sh(D节点)文件内。

    - **场景二**：通过Motor部署PD混部服务。

       无需额外修改，将上述网址中的PD混部部署脚本直接拷贝至 run_dp_template_hybrid.sh文件内。

4. 生成Motor配置。

    根据场景，执行以下命令直接生成Motor配置：

    ```bash
    # PD分离、Atlas 800I A2 推理服务器
    python3 deploy.py --mode general_config  --deploy-scenario separate --hardware-type A2
    # PD混部、Atlas 800I A2 推理服务器
    python3 deploy.py --mode general_config  --deploy-scenario hybrid --hardware-type A2
    # PD分离、Atlas 800I A3 超节点服务器
    python3 deploy.py --mode general_config --deploy-scenario separate --hardware-type A3
    # PD混部、Atlas 800I A3 超节点服务器
    python3 deploy.py --mode general_config  --deploy-scenario hybrid --hardware-type A3
    # PD分离、Atlas 850 超节点服务器
    python3 deploy.py --mode general_config --deploy-scenario separate --hardware-type A5
    # PD分离、Atlas 850 超节点服务器
    python3 deploy.py --mode general_config --deploy-scenario hybrid --hardware-type A5
    ```

5. 查看结果并微调。

   进入output_config目录，可以查看生成的user_config.json和env.json文件。

    ```bash
    cd examples/deployer/config_tool/output_config && ls
    ```

    user_config.json文件的以下内容需要用户根据实际情况手动填写：

    ```bash
    {
      "version": "v2.0",
      "motor_deploy_config": {
        ...
        "image_name": "<请手动填写镜像名称>",
        ...
        "weight_mount_path": "<请按实际情况填写模型权重文件的访问路径>"
      },
      ...
      "motor_engine_prefill_config": {
        ...
        "engine_config": {
          ...
          "model": "<请按实际情况填写模型权重文件的访问路径>",
          ...
        }
      },
      "motor_engine_decode_config": {
        ...
        "engine_config": {
          ...
          "model": "<请按实际情况填写模型权重文件的访问路径>",
          ...
        }
      }
    }
    ```

    env.json文件无需修改，至此，配置文件生成完成。
