# glm_5.3_flash · A5 · pd

来源：vLLM Ascend `GLM5.3-Flash.md` §5.3 950DT（提交 `9db9336d492e9a2bc6320355e2c803a4a9449592`，Prefill 代码块起始行 518 / Decode 起始行 582）。

场景：950DT 2 机 1P1D（Prefill DP1×TP8，Decode DP8×TP1，`MooncakeConnectorV2`，`kv-cache-dtype=fp8`）。本目录与 A2/A3 权重不可互换。填写镜像与权重路径后，按仓库 deployer 指导部署。

Motor 适配要点：不写 `nnodes`（PP1），不写 `api-server-count`。该配置完成静态转换与字段校验，未执行 NPU 启动验证。
