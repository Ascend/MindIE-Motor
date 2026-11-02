# DeepSeek-V4.1-Flash · A5

来源：vllm-ascend `releases/v0.30.0` [DeepSeek-V4.1-Flash.md](https://github.com/vllm-project/vllm-ascend/blob/releases/v0.30.0/docs/source/tutorials/models/DeepSeek-V4.1-Flash.md) 950DT 典配。

本目录默认 `user_config.json` / `env.json` 对齐仓库内 A3 示例形态，为 **950DT MemCache 池化 PD**（文档 §5.3）。A5 使用 `local_service_mode=inprocess`。填写镜像与权重路径后，按 deployer 指导部署。

| 子目录 | 场景 | 对应文档 |
| --- | --- | --- |
| 本目录默认文件 | MemCache 池化 PD | §5.3 |
| [pd/](./pd) | 无池化 PD | §5.2.1 |
| [hybrid/](./hybrid) | 单机混部 | §5.1 |

该配置完成静态转换与 K8s deployer 字段校验，未执行 NPU 启动、精度或性能验证。
