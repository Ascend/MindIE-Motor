# glm_5.3 · A2 · pd

来源：vLLM Ascend `releases/v0.30.0` `GLM5.2.md` / `GLM5.3-Flash.md` §5.1.3 A2 八机 PD。拓扑与本仓库 `glm_5.2/A2/pd` 相同（Prefill 4×8 卡 DP1×TP16×PP2，`nnodes=4`；Decode 4×8 卡 DP16×TP2）；PP 分区按 0.30.0 使用 `42,36`，`MooncakeConnectorV2`。

场景：A2 8-pod PD 分离。Motor 适配：Prefill 写 `nnodes=4` 与 `master-port`；Decode **不要**写 `nnodes`。`attention_config` 写成嵌套对象。不写 `api-server-count`。填写镜像与权重路径后，按仓库 deployer 指导部署。

该配置完成静态转换与字段校验，未执行 NPU 启动、精度或性能验证。
